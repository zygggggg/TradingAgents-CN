"""Training scaffold for future LightGBM/CatBoost quant models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd


FEATURE_COLUMNS = [
    "score",
    "factor_momentum",
    "factor_trend",
    "factor_risk",
    "factor_valuation",
    "factor_quality",
    "factor_liquidity",
]


@dataclass
class TrainingConfig:
    horizon: int = 20
    label_column: str = "return_20d"
    model_type: str = "lightgbm"
    test_size: float = 0.25
    random_state: int = 42


def build_training_frame(backtest_rows: pd.DataFrame, config: TrainingConfig) -> pd.DataFrame:
    """Build a supervised frame from rolling backtest rows."""

    missing = [column for column in FEATURE_COLUMNS + [config.label_column] if column not in backtest_rows.columns]
    if missing:
        raise ValueError(f"缺少训练列: {missing}")
    frame = backtest_rows[FEATURE_COLUMNS + [config.label_column]].copy()
    frame = frame.dropna()
    frame["label"] = frame[config.label_column]
    frame["label_positive"] = (frame["label"] > 0).astype(int)
    return frame


def train_model(training_frame: pd.DataFrame, config: TrainingConfig, model_path: Optional[Path] = None) -> Dict[str, Any]:
    """Train an optional LightGBM/CatBoost model when dependencies exist.

    This function is intentionally conservative: it refuses to fit tiny samples,
    because a single-stock backtest can easily overfit and look falsely good.
    """

    if len(training_frame) < 200:
        return train_model_placeholder(training_frame, config, reason="样本少于200条，暂不训练，避免过拟合。")

    X = training_frame[FEATURE_COLUMNS]
    y = training_frame["label"]

    if config.model_type == "lightgbm":
        try:
            import lightgbm as lgb
        except Exception as exc:
            return train_model_placeholder(training_frame, config, reason=f"未安装lightgbm: {exc}")
        model = lgb.LGBMRegressor(
            n_estimators=200,
            learning_rate=0.03,
            num_leaves=15,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=config.random_state,
        )
    elif config.model_type == "catboost":
        try:
            from catboost import CatBoostRegressor
        except Exception as exc:
            return train_model_placeholder(training_frame, config, reason=f"未安装catboost: {exc}")
        model = CatBoostRegressor(
            iterations=300,
            depth=4,
            learning_rate=0.03,
            loss_function="RMSE",
            random_seed=config.random_state,
            verbose=False,
        )
    else:
        raise ValueError(f"不支持的model_type: {config.model_type}")

    split_index = max(1, int(len(training_frame) * (1 - config.test_size)))
    X_train, X_test = X.iloc[:split_index], X.iloc[split_index:]
    y_train, y_test = y.iloc[:split_index], y.iloc[split_index:]
    model.fit(X_train, y_train)
    predictions = model.predict(X_test) if len(X_test) else []

    metrics = _regression_metrics(y_test, predictions)
    if model_path:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        _save_model(model, model_path, config.model_type)

    return {
        "model_type": config.model_type,
        "status": "trained",
        "sample_count": int(len(training_frame)),
        "train_count": int(len(X_train)),
        "test_count": int(len(X_test)),
        "feature_columns": FEATURE_COLUMNS,
        "label_column": config.label_column,
        "metrics": metrics,
        "model_path": str(model_path) if model_path else "",
    }


def train_model_placeholder(
    training_frame: pd.DataFrame,
    config: TrainingConfig,
    reason: str = "训练接口已预留；需要扩展到多股票池、更多历史样本后再训练，避免单票过拟合。",
) -> Dict[str, Any]:
    return {
        "model_type": config.model_type,
        "status": "not_trained_yet",
        "reason": reason,
        "sample_count": int(len(training_frame)),
        "feature_columns": FEATURE_COLUMNS,
        "label_column": config.label_column,
    }


def _regression_metrics(y_true: pd.Series, predictions: Any) -> Dict[str, Any]:
    if len(y_true) == 0:
        return {"message": "无测试集"}
    pred = pd.Series(predictions, index=y_true.index)
    errors = pred - y_true
    direction_hit = ((pred > 0) == (y_true > 0)).mean()
    return {
        "mae": round(float(errors.abs().mean()), 4),
        "rmse": round(float((errors.pow(2).mean()) ** 0.5), 4),
        "direction_hit_rate": round(float(direction_hit), 4),
        "prediction_mean": round(float(pred.mean()), 4),
        "actual_mean": round(float(y_true.mean()), 4),
    }


def _save_model(model: Any, model_path: Path, model_type: str) -> None:
    if model_type == "lightgbm":
        import joblib

        joblib.dump(model, model_path)
        return
    if model_type == "catboost":
        model.save_model(str(model_path))
        return
    raise ValueError(f"不支持保存model_type: {model_type}")
