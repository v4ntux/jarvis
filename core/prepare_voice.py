"""Explicit installation of local speech models, never microphone capture."""
import argparse
import sys

from core import settings as cfg


def configured_models():
    return list(dict.fromkeys((cfg.MODEL_WAKE, cfg.MODEL_MAIN)))


def prepare(check_only=False):
    from faster_whisper.utils import download_model

    failed = False
    for name in configured_models():
        print("Проверяю модель %s..." % name if check_only else "Готовлю модель %s..." % name,
              flush=True)
        try:
            download_model(name, local_files_only=check_only)
            print("  %s: готова." % name, flush=True)
        except Exception as exc:
            failed = True
            if check_only:
                print("  %s: нет в локальном кеше. Запустите prepare-voice.bat с интернетом." % name,
                      flush=True)
            else:
                print("  Не удалось установить %s: %s" % (name, exc), file=sys.stderr, flush=True)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Установка локальных моделей речи Jarvis")
    parser.add_argument("--check", action="store_true", help="только проверить кеш, без сети")
    args = parser.parse_args(argv)
    return prepare(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
