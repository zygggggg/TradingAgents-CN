"""Helpers for organizing generated reports by stock name/code."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_OUTPUTS = ROOT / "analysis_outputs"
RESULTS_ROOT = ROOT / "results"

SYMBOL_NAME_MAP = {
    "002410": "广联达",
    "002625": "光启技术",
    "603588": "高能环境",
    "603881": "数据港",
}


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "", str(name)).strip() or "股票"


def normalize_symbol(symbol: str) -> str:
    match = re.search(r"\d{6}", str(symbol))
    return match.group(0) if match else str(symbol).strip()


def stock_folder_name(symbol: str = "", stock_name: str = "") -> str:
    symbol = normalize_symbol(symbol) if symbol else ""
    name = stock_name or SYMBOL_NAME_MAP.get(symbol) or symbol or "未知股票"
    return safe_name(name)


def analysis_stock_dir(symbol: str = "", stock_name: str = "", *, create: bool = True) -> Path:
    directory = ANALYSIS_OUTPUTS / stock_folder_name(symbol, stock_name)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def results_stock_dir(symbol: str = "", stock_name: str = "", *, create: bool = True) -> Path:
    directory = RESULTS_ROOT / stock_folder_name(symbol, stock_name)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def candidate_analysis_dirs(symbol: str = "", stock_name: str = "") -> list[Path]:
    dirs: list[Path] = []
    for directory in [analysis_stock_dir(symbol, stock_name, create=False), ANALYSIS_OUTPUTS]:
        if directory not in dirs:
            dirs.append(directory)
    return dirs


def candidate_result_dirs(symbol: str = "", stock_name: str = "") -> list[Path]:
    dirs: list[Path] = []
    symbol = normalize_symbol(symbol) if symbol else ""
    for directory in [results_stock_dir(symbol, stock_name, create=False), RESULTS_ROOT / symbol, RESULTS_ROOT]:
        if directory not in dirs:
            dirs.append(directory)
    return dirs
