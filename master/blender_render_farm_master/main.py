from __future__ import annotations

from pathlib import Path
import sys
import traceback
import ctypes

from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.coordinator_manager import CoordinatorManager
from app.core.job_manager import JobManager
from app.services.logger import AppLogger
from app.services.persistence import PersistenceService
from app.ui.main_window import MainWindow
from app.ui.theme import apply_app_theme


def is_frozen_build() -> bool:
    return bool(getattr(sys, "frozen", False))


def sync_windows_icon_assets(base_dir: Path, logger: AppLogger | None = None) -> Path | None:
    png_path = base_dir / "img" / "icon_master.png"
    ico_path = base_dir / "img" / "icon_master.ico"
    if not png_path.exists():
        return ico_path if ico_path.exists() else None
    needs_refresh = not ico_path.exists()
    if not needs_refresh:
        needs_refresh = png_path.stat().st_mtime > ico_path.stat().st_mtime
    if needs_refresh:
        saved = False
        try:
            from PIL import Image

            image = Image.open(png_path).convert("RGBA")
            image.save(
                ico_path,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
            saved = True
        except Exception:
            image = QImage(str(png_path))
            if image.isNull():
                if logger:
                    logger.warning(f"Nao foi possivel carregar o PNG do icone: {png_path}")
            else:
                saved = image.save(str(ico_path), "ICO")
        if saved:
            if logger:
                logger.info(f"Icone ICO sincronizado a partir de {png_path.name}")
        elif logger:
            logger.warning(f"Nao foi possivel salvar o ICO do icone em {ico_path}")
    return ico_path if ico_path.exists() else png_path


def resolve_app_icon(base_dir: Path, logger: AppLogger | None = None) -> QIcon:
    synced_icon = None
    if not is_frozen_build():
        synced_icon = sync_windows_icon_assets(base_dir=base_dir, logger=logger)
    icon_candidates = [candidate for candidate in [synced_icon, base_dir / "img" / "icon_master.png"] if candidate]
    for path in icon_candidates:
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "blender.renderfarm.master"
        )
    except Exception:
        pass


def build_runtime_manager(
    settings,
    persistence: PersistenceService,
    logger: AppLogger,
):
    if settings.is_coordinator_mode:
        return CoordinatorManager(settings=settings, persistence=persistence, logger=logger)
    return JobManager(settings=settings, persistence=persistence, logger=logger)


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
        "Blender Render Farm Master startup error",
        "",
        f"type: {type(exc).__name__}",
        f"message: {exc}",
        "",
        traceback.format_exc(),
    ]
    cause = exc.__cause__
    if cause is not None:
        details.extend(
            [
                "",
                f"cause_type: {type(cause).__name__}",
                f"cause_message: {cause}",
                "",
                "".join(traceback.format_exception(type(cause), cause, cause.__traceback__)),
            ]
        )
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

    config_path = runtime_dir / "config.json"
    persistence = PersistenceService(base_dir=runtime_dir, config_path=config_path)
    settings = persistence.load_settings()
    logger = AppLogger(persistence.logs_dir)
    job_manager = build_runtime_manager(settings=settings, persistence=persistence, logger=logger)

    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    apply_app_theme(app)
    app_icon = resolve_app_icon(base_dir=resource_dir, logger=logger)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error(f"Exceção não tratada: {exc_value}")
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        QMessageBox.critical(None, "Erro inesperado", str(exc_value))

    sys.excepthook = handle_exception

    window = MainWindow(
        base_dir=runtime_dir,
        job_manager=job_manager,
        persistence=persistence,
        logger=logger,
        default_block_size=settings.default_block_size,
        default_worker_port=settings.default_worker_port,
        poll_interval_ms=settings.worker_poll_interval_ms,
    )
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in {0, None}:
            raise
        write_startup_error(resolve_runtime_dir(), exc)
        raise
