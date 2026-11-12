"""Persistent user configuration (JSON under %APPDATA%/VoiceTray)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from . import manifest


def app_dir() -> Path:
    """Root that contains `assets/`.

    In development this is the project root. In a PyInstaller 6 onedir build
    the bundled data lives under `_internal/`, which is exactly sys._MEIPASS;
    using the exe directory instead silently misses the weights.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_model_dir() -> Path:
    return app_dir() / "assets" / "models" / manifest.MODEL_ID


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA") or str(Path.home())) / "VoiceTray"
    return base / "config.json"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    hotkey: str = "rmenu"                 # first release only supports rmenu
    device: Optional[int] = None          # None = auto (prefer native 16k)
    sample_rate: int = 16000
    language: str = "auto"                # auto / zh / en / ja / ko / yue
    use_itn: bool = True
    num_threads: int = 2
    max_record_seconds: float = 60.0
    allow_injected: bool = True           # needed for automated UI acceptance
    use_uia: bool = True                  # UIA RuntimeId in target fingerprint
    model_dir: str = ""
    show_status_window: bool = True

    def __post_init__(self) -> None:
        if not self.model_dir:
            self.model_dir = str(default_model_dir())

    def validate(self) -> list[str]:
        """Return human-readable problems; empty list means valid."""
        problems: list[str] = []
        if self.hotkey not in ("rmenu",):
            problems.append("hotkey 目前只支持 rmenu（右 Alt）")
        if not (8000 <= self.sample_rate <= 48000):
            problems.append("sample_rate 必须在 8000..48000")
        if self.language not in ("auto", "zh", "en", "ja", "ko", "yue"):
            problems.append("language 取值非法")
        if not (1 <= self.num_threads <= 16):
            problems.append("num_threads 必须在 1..16")
        if not (1.0 <= self.max_record_seconds <= 300.0):
            problems.append("max_record_seconds 必须在 1..300")
        if self.device is not None and self.device < 0:
            problems.append("device 必须为非负索引或 null")
        return problems

    @classmethod
    def load(cls, path: Optional[Path] = None) -> tuple["Config", list[str]]:
        """Load config, falling back per-field on bad values.

        Returns (config, warnings) so a corrupt file never prevents startup.
        """
        p = Path(path) if path else config_path()
        cfg = cls()
        warnings: list[str] = []
        if not p.is_file():
            return cfg, warnings
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return cfg, [f"配置文件读取失败，已使用默认值：{e}"]
        if not isinstance(raw, dict):
            return cfg, ["配置文件不是对象，已使用默认值"]
        allowed = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key not in allowed:
                warnings.append(f"忽略未知配置项 {key}")
                continue
            expected = getattr(cfg, key)
            try:
                if isinstance(expected, bool):
                    if not isinstance(value, bool):
                        raise ValueError("not a bool")
                elif isinstance(expected, int):
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError("not an int")
                elif isinstance(expected, float):
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError("not a number")
                    value = float(value)
                elif isinstance(expected, str):
                    if not isinstance(value, str):
                        raise ValueError("not a str")
                elif expected is None:
                    if value is not None and (isinstance(value, bool)
                                              or not isinstance(value, int)):
                        raise ValueError("not an int or null")
                setattr(cfg, key, value)
            except (ValueError, TypeError):
                warnings.append(f"配置项 {key} 取值非法，已保留默认值")
        # A config written by a previous install (or a moved exe) may point
        # at a model directory that no longer exists. Fall back to the
        # bundled one instead of failing every transcription.
        if cfg.model_dir and not Path(cfg.model_dir).is_dir():
            warnings.append("配置中的模型目录不存在，已改用随包模型目录")
            cfg.model_dir = str(default_model_dir())
        problems = cfg.validate()
        if problems:
            cfg = cls()
            warnings.extend(problems)
            warnings.append("已整体回退到默认配置")
        return cfg, warnings

    def save(self, path: Optional[Path] = None) -> Path:
        problems = self.validate()
        if problems:
            raise ConfigError("; ".join(problems))
        p = Path(path) if path else config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p
