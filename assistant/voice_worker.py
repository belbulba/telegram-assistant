"""Invoked directly with the isolated speech venv, outside the bot process."""
import json
import os
import sys


def main():
    os.nice(10)
    from faster_whisper import WhisperModel
    model = WhisperModel(sys.argv[1], device='cpu', compute_type='int8',
                         cpu_threads=1, num_workers=1, local_files_only=True)
    segments, _ = model.transcribe(sys.argv[2], language='ru', beam_size=1,
                                    vad_filter=False, condition_on_previous_text=False)
    print(json.dumps({'text': ''.join(part.text for part in segments)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
