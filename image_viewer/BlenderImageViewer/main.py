from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen

from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from app.ui.main_window import MainWindow
from app.ui.theme import apply_app_theme


def is_frozen_build() -> bool:
    return bool(getattr(sys, "frozen", False))


def icon_png_candidates(base_dir: Path) -> list[Path]:
    return [
        base_dir / "img" / "icon.png",
        base_dir / "img" / "icon_master.png",
    ]


def sync_windows_icon_assets(base_dir: Path, logger: AppLogger | None = None) -> Path | None:
    png_path = next((candidate for candidate in icon_png_candidates(base_dir) if candidate.exists()), None)
    ico_path = base_dir / "img" / "icon.ico"
    legacy_ico_path = base_dir / "img" / "icon_master.ico"
    if not png_path:
        if ico_path.exists():
            return ico_path
        return legacy_ico_path if legacy_ico_path.exists() else None
    needs_refresh = not ico_path.exists()
    if not needs_refresh:
        needs_refresh = png_path.stat().st_mtime > ico_path.stat().st_mtime
    if needs_refresh:
        saved = False
        image = QImage(str(png_path))
        if not image.isNull():
            saved = image.save(str(ico_path), "ICO")
        if saved and logger:
            logger.info(f"Icone ICO sincronizado a partir de {png_path.name}")
    return ico_path if ico_path.exists() else png_path


def resolve_app_icon(base_dir: Path, logger: AppLogger | None = None) -> QIcon:
    synced_icon = None
    if not is_frozen_build():
        synced_icon = sync_windows_icon_assets(base_dir=base_dir, logger=logger)
    icon_candidates = [
        candidate
        for candidate in [
            synced_icon,
            base_dir / "img" / "icon.ico",
            base_dir / "img" / "icon_master.ico",
            *icon_png_candidates(base_dir),
        ]
        if candidate
    ]
    for path in icon_candidates:
        if path and path.exists():
            return QIcon(str(path))
    return QIcon()


def resolve_splash_pixmap(base_dir: Path, logger: AppLogger | None = None) -> QPixmap:
    splash_path = base_dir / "img" / "splash.png"
    if not splash_path.exists():
        return QPixmap()

    pixmap = QPixmap(str(splash_path))
    if pixmap.isNull():
        if logger:
            logger.warning(f"Splash invalida ou nao carregavel: {splash_path}")
        return QPixmap()

    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return pixmap

    available = screen.availableGeometry().size()
    max_width = max(420, int(available.width() * 0.54))
    max_height = max(260, int(available.height() * 0.54))
    if pixmap.width() > max_width or pixmap.height() > max_height:
        return pixmap.scaled(max_width, max_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pixmap


def create_startup_splash(base_dir: Path, app_icon: QIcon, logger: AppLogger | None = None) -> QSplashScreen | None:
    pixmap = resolve_splash_pixmap(base_dir=base_dir, logger=logger)
    if pixmap.isNull():
        return None

    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint | Qt.SplashScreen)
    if not app_icon.isNull():
        splash.setWindowIcon(app_icon)
    splash.setEnabled(False)
    splash.show()
    QApplication.processEvents()
    return splash


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("blender.renderfarm.imageviewer")
    except Exception:
        pass


def resolve_runtime_dir() -> Path:
    if is_frozen_build():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_resource_dir(runtime_dir: Path) -> Path:
    if is_frozen_build():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return runtime_dir


def write_startup_error(runtime_dir: Path, exc: BaseException) -> None:
    error_path = runtime_dir / "startup_error.txt"
    details = [
        "BlenderImageViewer startup error",
        "",
        f"type: {type(exc).__name__}",
        f"message: {exc}",
        "",
        traceback.format_exc(),
    ]
    error_path.write_text("\n".join(details), encoding="utf-8")


def main() -> int:
    smoke_test = "--smoke-test" in sys.argv
    runtime_dir = resolve_runtime_dir()
    resource_dir = resolve_resource_dir(runtime_dir)

    if smoke_test:
        set_windows_app_user_model_id()
        app = QApplication(sys.argv)
        probe = QImage(2, 2, QImage.Format_ARGB32)
        if probe.isNull():
            raise RuntimeError("QtGui smoke test could not allocate an image")
        return 0

    persistence = PersistenceService(base_dir=runtime_dir, config_path=runtime_dir / "config.json")
    settings = persistence.load_settings()
    logger = AppLogger(persistence.logs_dir)

    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    apply_app_theme(app)
    app_icon = resolve_app_icon(base_dir=resource_dir, logger=logger)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    splash = create_startup_splash(base_dir=resource_dir, app_icon=app_icon, logger=logger)
    splash_timer = QElapsedTimer()
    splash_timer.start()

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error(f"Excecao nao tratada: {exc_value}")
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        QMessageBox.critical(None, "Erro inesperado", str(exc_value))

    sys.excepthook = handle_exception

    window = MainWindow(
        base_dir=runtime_dir,
        settings=settings,
        persistence=persistence,
        logger=logger,
    )
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    if splash is not None:
        remaining_ms = max(0, 850 - splash_timer.elapsed())
        if remaining_ms > 0:
            QTimer.singleShot(remaining_ms, lambda: splash.finish(window))
        else:
            splash.finish(window)
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in {0, None}:
            raise
        write_startup_error(resolve_runtime_dir(), exc)
        raise
