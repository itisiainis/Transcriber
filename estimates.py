"""Runtime predictions shared by the panel and batch queue.

Only explicitly profiled runs train Whisper estimates. Legacy CPU/GPU runs
cannot be distinguished reliably and are deliberately not backfilled.
"""
import json
import math
import socket
import statistics
import time
from pathlib import Path


def _stamp(path):
    path = Path(path)
    try:
        stat = path.stat()
        return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [str(path.resolve()), None, None]


def profile(values=None):
    import settings
    import transcribe as tx
    s = values if values is not None else settings.all()
    return json.dumps({
        'machine': socket.gethostname(), 'backend': 'vulkan' if s['whisper_gpu'] else 'cpu',
        'model': _stamp(s['whisper_model']),
        'executable': _stamp(tx.WHISPER_GPU_EXE if s['whisper_gpu'] else tx.WHISPER_EXE),
        'threads': s['whisper_threads'], 'language': s['language'],
        'vad': s['vad_enabled'], 'vad_threshold': s['vad_threshold'] if s['vad_enabled'] else None,
        'vad_model': _stamp(tx.WHISPER_VAD_MODEL) if s['vad_enabled'] else None,
        'beam': tx.WHISPER_BEAM, 'best_of': tx.WHISPER_BEST_OF,
    }, sort_keys=True)


def decode(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (ValueError, TypeError):
        return default if default is not None else {}


def _median(values, fallback):
    values = [v for v in values if v is not None and math.isfinite(v) and v >= 0]
    return statistics.median(values) if values else fallback


def fit(samples, key):
    """Robust fixed startup + per-audio-second cost; recent matching runs only."""
    gpu = decode(key).get('backend') == 'vulkan'
    samples = samples[-40:]
    startup = _median([p.get('startup') for p in samples], 2.0)
    rates = [(p['elapsed'] - (p.get('startup') or 0)) / p['work_seconds']
             for p in samples if (p.get('work_seconds') or 0) > 0
             and p['elapsed'] > (p.get('startup') or 0)]
    center = _median(rates, .04 if gpu else .25)
    # A stuck run or a suspiciously fast decode must not dominate predictions.
    rates = [r for r in rates if center / 3 <= r <= center * 3]
    rate = _median(rates, center)
    scatter = _median([abs(r - rate) for r in rates], rate * .5)
    uncertainty = max(.2, min(1.0, 2 * scatter / max(rate, 1e-9)))
    if len(rates) < 3:
        uncertainty = max(uncertainty, .75)
    return {'startup': startup, 'rate': rate, 'uncertainty': uncertainty, 'count': len(rates)}


def load(conn, key=None):
    key = key or profile()
    rows = conn.execute(
        """SELECT s.performance, s.t_whisper, s.t_download, s.t_metadata, s.t_captions,
                  v.platform, v.duration
           FROM submissions s JOIN videos v USING(video_id)
           WHERE s.status = 'done' ORDER BY s.id DESC LIMIT 500"""
    ).fetchall()
    samples = []
    for row in reversed(rows):
        p = decode(row['performance'])
        if p.get('profile') == key and p.get('eligible') and row['t_whisper']:
            samples.append({**p, 'elapsed': row['t_whisper']})
    model = fit(samples, key)
    model['speech_fraction'] = _median([
        decode(r['performance']).get('speech_fraction') for r in rows
        if decode(r['performance']).get('profile') == key
        and decode(r['performance']).get('eligible')], 1.0)
    # Network downloads and local WAV conversion have very different costs.
    model['conversion_rate'] = _median([r['t_download'] / r['duration'] for r in rows
        if r['platform'] == 'recording' and r['duration'] and r['t_download'] is not None], .002)
    model['download_rate'] = _median([r['t_download'] / r['duration'] for r in rows
        if r['platform'] != 'recording' and r['duration'] and r['t_download'] is not None], .03)
    model['metadata'] = _median([r['t_metadata'] for r in rows if r['platform'] != 'recording'], 3)
    model['captions'] = _median([r['t_captions'] for r in rows], 3)
    return model


def total(duration, platform_name, model):
    if not duration:
        return None
    preparation = duration * model['conversion_rate' if platform_name == 'recording' else 'download_rate']
    overhead = 0 if platform_name == 'recording' else model['metadata']
    if platform_name == 'youtube':
        overhead += model['captions']
    return overhead + preparation + model['startup'] + duration * model['speech_fraction'] * model['rate'] + .5


def progress(job, duration, model, platform_name, now=None):
    now = time.time() if now is None else now
    stage = job['stage'] or 'queued'
    live = decode(job['progress_json'])
    phase = live.get('phase', 'loading') if stage == 'whisper' else stage
    result = {'stage': stage, 'phase': phase, 'reported_at': now,
              'fraction': None, 'remaining': None, 'low': None, 'high': None}
    if stage in ('metadata', 'captions'):
        # Captions may finish the entire job; don't promise a Whisper ETA yet.
        return result
    elapsed = max(0, now - (job['stage_started_at'] or now))
    spread = model['uncertainty']
    work = live.get('work_seconds') or ((duration or 0) * model['speech_fraction'])
    if stage == 'saving':
        return result  # indeterminate until the job actually completes
    if not work:
        return result
    if stage == 'download':
        rate = model['conversion_rate' if platform_name == 'recording' else 'download_rate']
        result['phase'] = 'converting' if platform_name == 'recording' else 'download'
        left = max(0, (duration or work) * rate - elapsed) + model['startup'] + work * model['rate'] + .5
        spread = max(spread, .5)
    elif stage == 'whisper':
        pct = live.get('pct')
        samples = live.get('samples', [])
        if pct is not None:
            result['fraction'] = min(pct / 100, .99)
        if pct is not None and pct >= 100:
            result['phase'] = 'finishing'
            return result
        fraction = (pct or 0) / 100
        cost = work * model['rate'] * (1 - fraction)
        rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(samples, samples[1:])
                 if b[0] > a[0] and b[1] > a[1]]
        if rates:
            recent = statistics.median(rates[-6:]) * (100 - pct)
            weight = min(.9, (samples[-1][0] - samples[0][0]) / 30)
            cost = weight * recent + (1 - weight) * cost
            spread = max(.2, spread * (1 - weight))
        anchor = live.get('updated_at', now) if pct is not None else live.get('processing_at', now)
        age = max(0, now - anchor)
        if phase not in ('loading', 'preparing') and age > max(5, cost * (1 + spread) + 2):
            result['phase'] = 'waiting_progress'
            return result
        loading_left = max(0, model['startup'] - elapsed) if phase in ('loading', 'preparing') else 0
        left = max(0, cost - age) + loading_left + .5
    else:
        return result
    margin = max(2, left * spread)
    result.update(remaining=left, low=max(0, left - margin), high=left + margin)
    return result


def backtest(conn):
    """Chronological validation: each prediction uses earlier matching runs only."""
    groups, errors, covered = {}, [], []
    for row in conn.execute('SELECT performance, t_whisper FROM submissions WHERE status = ? ORDER BY id', ('done',)):
        p = decode(row['performance'])
        if not p.get('eligible') or not p.get('work_seconds') or not row['t_whisper']:
            continue
        history = groups.setdefault(p['profile'], [])
        if len(history) >= 3:
            model = fit(history, p['profile'])
            prediction = model['startup'] + p['work_seconds'] * model['rate']
            error = abs(prediction - row['t_whisper'])
            errors.append(error)
            covered.append(error <= max(2, prediction * model['uncertainty']))
        history.append({**p, 'elapsed': row['t_whisper']})
    return {'predictions': len(errors), 'median_error_seconds': statistics.median(errors) if errors else None,
            'range_coverage': sum(covered) / len(covered) if covered else None}


if __name__ == '__main__':
    from contextlib import closing
    import store
    with closing(store.connect()) as conn:
        print(json.dumps(backtest(conn), indent=2))
