import io
import json
from pathlib import Path
import sys
import tempfile
import wave
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings
import store
import transcribe as tx


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        connections = []
        connect = store.connect
        def isolated_connect(*args, **kwargs):
            conn = connect(self.root / 'test.db')
            connections.append(conn)
            return conn
        self.addCleanup(lambda: [conn.close() for conn in connections])
        p = patch.object(store, 'connect', side_effect=isolated_connect)
        p.start()
        self.addCleanup(p.stop)
        for target, name, value in [(store, 'DB_PATH', self.root / 'test.db'),
                                    (tx, 'WHISPER_EXE', self.root / 'cpu.exe'),
                                    (tx, 'WHISPER_GPU_EXE', self.root / 'gpu.exe'),
                                    (tx, '_HELP_CACHE', {})]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        tx.WHISPER_EXE.touch()
        tx.WHISPER_GPU_EXE.touch()
        self.wav = self.root / 'clip.wav'
        self.wav.with_suffix('.json').write_text(json.dumps({
            'result': {'language': 'en'}, 'transcription': [
                {'offsets': {'from': 0, 'to': 1000}, 'text': 'Test.'}]}))

    def run_audio(self, gpu, stderr=''):
        proc = Mock(stderr=io.StringIO(stderr), returncode=0)
        proc.wait.return_value = 0
        with settings.override(whisper_gpu=gpu, vad_enabled=False), \
             patch.object(tx, 'whisper_supports', return_value=True), \
             patch.object(tx.subprocess, 'Popen', return_value=proc) as popen:
            result = tx.transcribe_audio(self.wav)
        return result, popen.call_args.args[0]

    def test_gpu_setting_validates_persists_and_resets(self):
        self.assertFalse(settings.get('whisper_gpu'))
        settings.set('whisper_gpu', True)
        self.assertTrue(settings.get('whisper_gpu'))
        with self.assertRaises(ValueError):
            settings.set('whisper_gpu', 'true')
        settings.reset('whisper_gpu')
        self.assertFalse(settings.get('whisper_gpu'))

    def test_toggle_selects_gpu_then_cpu_for_next_run(self):
        _, gpu = self.run_audio(True, 'whisper_backend_init_gpu: using Vulkan0 backend\n')
        _, cpu = self.run_audio(False)
        self.assertEqual(gpu[0], str(tx.WHISPER_GPU_EXE))
        self.assertNotIn('--no-gpu', gpu)
        self.assertEqual(cpu[0], str(tx.WHISPER_EXE))
        self.assertIn('--no-gpu', cpu)

    def test_missing_gpu_build_explains_setup(self):
        tx.WHISPER_GPU_EXE.unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'build-whisper-vulkan'):
            self.run_audio(True)

    def test_gpu_fallback_is_not_reported_as_success(self):
        for diagnostics in [
            'whisper_backend_init_gpu: no GPU found\n',
            'whisper_backend_init_gpu: using Vulkan0 backend\n'
            'whisper_backend_init_gpu: failed to initialize Vulkan0 backend\n',
        ]:
            with self.subTest(diagnostics=diagnostics), self.assertRaisesRegex(RuntimeError, 'did not activate'):
                self.run_audio(True, diagnostics)

    def test_help_cache_is_per_executable(self):
        with patch.object(tx.subprocess, 'run', side_effect=[
            Mock(stdout='--no-gpu', stderr=''), Mock(stdout='--vad', stderr='')]) as run:
            self.assertTrue(tx.whisper_supports('--no-gpu', tx.WHISPER_EXE))
            self.assertFalse(tx.whisper_supports('--no-gpu', tx.WHISPER_GPU_EXE))
            self.assertTrue(tx.whisper_supports('--vad', tx.WHISPER_GPU_EXE))
            self.assertEqual(run.call_count, 2)

    def test_recording_title_stays_metadata_and_does_not_prime_decoder(self):
        metadata = {'video_id': 'r0123456789abcde', 'platform': 'recording',
                    'title': 'Урок. 3. Мастер-класс «От задачи до результата»',
                    'channel': 'Course channel', 'duration': 90}
        proc = Mock(stderr=io.StringIO('whisper_backend_init_gpu: using Vulkan0 backend\n'), returncode=0)
        proc.wait.return_value = 0
        with settings.override(whisper_gpu=True, vad_enabled=False), \
             patch.object(tx, 'fetch_metadata', return_value=metadata), \
             patch.object(tx, 'download_audio', return_value=self.wav), \
             patch.object(tx, 'whisper_supports', return_value=True), \
             patch.object(tx.subprocess, 'Popen', return_value=proc) as popen:
            result = tx.transcribe('recording:r0123456789abcde')
        self.assertEqual(result['title'], metadata['title'])
        self.assertEqual(result['segments'][0]['text'], 'Test.')
        self.assertNotIn('--prompt', popen.call_args.args[0])

    def test_runtime_reports_progress_vad_workload_and_profile(self):
        with wave.open(str(self.wav), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 16000)
        vad_model = self.root / 'vad.bin'
        vad_model.touch()
        proc = Mock(stderr=io.StringIO(
            'whisper_backend_init_gpu: using Vulkan0 backend\n'
            'main: processing audio\n'
            'whisper_vad: Reduced audio from 16000 to 8000 samples\n'
            'progress = 10%\nprogress = 20%\nprogress = 20%\nprogress = 15%\nprogress = 100%\n'),
            returncode=0)
        proc.wait.return_value = 0
        status, progress = [], []
        with settings.override(whisper_gpu=True, vad_enabled=True), \
             patch.object(tx, 'WHISPER_VAD_MODEL', vad_model), \
             patch.object(tx, 'whisper_supports', return_value=True), \
             patch.object(tx.subprocess, 'Popen', return_value=proc):
            result = tx.transcribe_audio(self.wav, on_progress=progress.append, on_status=status.append)
        self.assertEqual(progress, [10, 20, 100])
        self.assertEqual([s['phase'] for s in status], ['loading', 'preparing', 'transcribing'])
        self.assertEqual(result['performance']['work_seconds'], .5)
        self.assertEqual(result['performance']['speech_fraction'], .5)
        self.assertEqual(json.loads(result['performance']['profile'])['backend'], 'vulkan')
        self.assertGreaterEqual(result['performance']['startup'], 0)

    def test_repeated_numbered_lines_do_not_train_speed_estimates(self):
        self.wav.with_suffix('.json').write_text(json.dumps({
            'result': {'language': 'en'}, 'transcription': [
                {'offsets': {'from': i * 1000, 'to': (i + 1) * 1000},
                 'text': f'{i}. This invented line is repeated over and over.'} for i in range(8)]}))
        result, _ = self.run_audio(False)
        self.assertFalse(result['performance']['eligible'])


if __name__ == '__main__':
    unittest.main()
