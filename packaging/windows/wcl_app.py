"""Точка входа Windows-программы: двойной щелчок — интерфейс открывается в браузере."""
import sys

try:  # в окне консоли сообщения должны появляться сразу
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from wcl_analyzer.__main__ import main  # noqa: E402

if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # окно не должно закрываться молча
        print(f"Ошибка: {e}")
        input("Нажмите Enter, чтобы закрыть окно…")
        raise
