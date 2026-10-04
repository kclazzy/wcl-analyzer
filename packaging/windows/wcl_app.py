"""Точка входа Windows-программы: двойной щелчок — интерфейс открывается в браузере."""
import sys

try:  # в окне консоли сообщения должны появляться сразу
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import wcl_boot  # noqa: E402 — скачанное обновление кода, если есть, подключается до импорта программы

_build = wcl_boot.activate()
if _build:
    print(f"Обновлённая версия кода: сборка {_build}")


def _run(argv):
    from wcl_analyzer.__main__ import main  # после отката — уже встроенная версия
    main(argv)


if __name__ == "__main__":
    try:
        try:
            _run(sys.argv[1:])
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:  # скачанный код упал — один раз пробуем встроенную версию
            if not wcl_boot.fall_back(e):
                raise
            _run(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # окно не должно закрываться молча
        print(f"Ошибка: {e}")
        input("Нажмите Enter, чтобы закрыть окно…")
        raise
