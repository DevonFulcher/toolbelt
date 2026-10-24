"""The challenger models: linear quantile regression, a discrete-time hazard
(survival) model and gradient-boosted quantile trees. They learn from the
snapshot rows (``snapshots``), so they use every registry feature, the time
already spent and the right-censored open tasks; each answers ``predict`` with
p50 and p80 of the working time left to the task's finish, and says what it
learned. scikit-learn is imported lazily, inside ``fit``/``predict`` only.

Targets are modelled in log seconds (quantiles commute with a monotone
transform). A censored row (a task still open: its target is a lower bound) is
handled in two passes for the quantile models: first it counts as observed at
its bound; the models are fitted; a censored row whose bound is below the first
fit's prediction could end anywhere above it, so it is dropped and the models
are fitted again. A censored row above the prediction is a certain "above" and
is kept as is. The hazard model uses censoring natively.

Every fit is deterministic (fixed ``random_state``). No model answers for a
stage with fewer than ``MIN_STAGE_ROWS`` training rows, or at all with fewer
than ``MIN_ROWS``: it says unknown rather than extrapolating.
"""

import math
import pickle
from collections import Counter
from collections.abc import Sequence
from typing import Any

lazy import numpy as np

from toolbelt.finish_estimate.features import BY_NAME, REGISTRY, Feature
from toolbelt.finish_estimate.forecast import Forecast, TrainingData
from toolbelt.finish_estimate.model import LEVELS
from toolbelt.finish_estimate.snapshots import SnapshotFeatures, SnapshotRow

#: The latest this many snapshot rows train a model (the others are older).
ROW_CAP = 50_000
MIN_ROWS = 60
MIN_STAGE_ROWS = 10
_FLOOR = 60.0  # seconds, before a logarithm

#: Working-hour bin edges of the hazard model; the last bin is open.
HAZARD_EDGES = (0, 1, 2, 4, 8, 16, 24, 40, 60, 80, 120, 160, 240, 400, 800)


def _unknown(name: str, n: int, why: str) -> Forecast:
    return Forecast(
        model=name,
        p50_seconds=None,
        p80_seconds=None,
        level="unknown",
        n=n,
        text=f"unknown: {why}",
    )


class _Design:
    """The model matrix of one stage: the elapsed time and every numeric
    registry feature that applies to it (log features as ``log1p``; a missing
    value is the training mean plus an indicator), standardized on the
    training rows. It holds feature names only (so it pickles)."""

    def __init__(
        self, exclude: frozenset[str], stage: str, elapsed: bool = True
    ) -> None:
        self.elapsed = elapsed
        self.exclude = exclude
        self.stage = stage
        self.names_: list[str] = []
        self.mean: dict[str, float] = {}
        self.std: dict[str, float] = {}
        self.missing: set[str] = set()

    @property
    def features(self) -> list[Feature]:
        return [BY_NAME[n] for n in self.names_]

    @staticmethod
    def _raw(feature: Feature, state: SnapshotFeatures) -> float | None:
        value = state.values.get(feature.name)
        if value is None or value == "":
            return None
        x = float(value)
        return math.log1p(x) if feature.encoding == "log" else x

    def learn(self, states: Sequence[SnapshotFeatures]) -> None:
        self.names_ = [
            f.name
            for f in REGISTRY
            if f.encoding != "categorical"
            and f.applies(self.stage)
            and f.name not in self.exclude
        ]
        for f in self.features:
            values = [v for s in states if (v := self._raw(f, s)) is not None]
            if len(values) < len(states):
                self.missing.add(f.name)
            mean = sum(values) / len(values) if values else 0.0
            var = sum((v - mean) ** 2 for v in values) / len(values) if values else 0.0
            self.mean[f.name] = mean
            self.std[f.name] = math.sqrt(var) or 1.0
        elapsed = [math.log1p(s.elapsed_seconds / 3600) for s in states]
        self.mean["elapsed"] = sum(elapsed) / len(elapsed)
        self.std["elapsed"] = (
            math.sqrt(
                sum((v - self.mean["elapsed"]) ** 2 for v in elapsed) / len(elapsed)
            )
            or 1.0
        )

    def names(self) -> list[str]:
        return [
            "elapsed",
            *self.names_,
            *(f"{n} missing" for n in sorted(self.missing)),
        ]

    def matrix(self, states: Sequence[SnapshotFeatures]) -> np.ndarray:
        rows = []
        for s in states:
            row = [
                (math.log1p(s.elapsed_seconds / 3600) - self.mean["elapsed"])
                / self.std["elapsed"]
            ]
            flags = []
            for f in self.features:
                x = self._raw(f, s)
                row.append(
                    0.0 if x is None else (x - self.mean[f.name]) / self.std[f.name]
                )
                if f.name in self.missing:
                    flags.append((f.name, 1.0 if x is None else 0.0))
            row += [v for _, v in sorted(flags)]
            rows.append(row)
        return np.array(rows, dtype=float)


def _training_rows(data: TrainingData) -> list[SnapshotRow]:
    rows = [r for r in data.rows if r.finish_remaining_seconds is not None]
    return rows[-ROW_CAP:]


def _log_target(row: SnapshotRow) -> float:
    return math.log(max(row.finish_remaining_seconds or 0.0, _FLOOR))


def _two_pass(fit, x: np.ndarray, y: np.ndarray, censored: np.ndarray) -> Any:
    """``fit(x, y)`` -> model with ``predict``; handles the censored rows as
    the module docstring says."""
    model = fit(x, y)
    if censored.any():
        pred = model.predict(x)
        keep = ~censored | (y > pred)
        if keep.sum() >= MIN_ROWS and keep.sum() < len(y):
            model = fit(x[keep], y[keep])
    return model


class _Learned:
    """What the per-stage models share: the rows of a stage train that
    stage's part, a stage with too few rows answers unknown, ``dump``/``load``."""

    name = ""
    version = 1

    def __init__(self, exclude: frozenset[str] = frozenset()) -> None:
        self.exclude = exclude
        self.trained_on = 0
        self.stage_rows: Counter[str] = Counter()
        self.parts: dict[str, Any] = {}

    def fit(self, data: TrainingData) -> None:
        rows = _training_rows(data)
        self.trained_on = len(rows)
        self.stage_rows = Counter(r.features.stage for r in rows)
        self.parts = {}
        for stage, count in sorted(self.stage_rows.items()):
            if count >= MIN_STAGE_ROWS:
                self.parts[stage] = self._fit_stage(
                    stage, [r for r in rows if r.features.stage == stage]
                )

    def _fit_stage(self, stage: str, rows: list[SnapshotRow]) -> Any:
        raise NotImplementedError

    def dump(self) -> bytes | None:
        return pickle.dumps(self.__dict__) if self.parts else None

    def load(self, state: bytes) -> None:
        self.__dict__.update(pickle.loads(state))

    def _quantiles(self, part: Any, x: np.ndarray) -> tuple[float, float]:
        raise NotImplementedError

    def predict(self, state: SnapshotFeatures) -> Forecast:
        part = self.parts.get(state.stage)
        if part is None:
            rows = self.stage_rows[state.stage]
            return _unknown(
                self.name, rows, f"no history for this stage yet ({rows} rows)"
            )
        x = part["design"].matrix([state])
        p50, p80 = self._quantiles(part, x)
        p80 = max(p80, p50)  # quantiles never cross
        return Forecast(
            model=self.name,
            p50_seconds=p50,
            p80_seconds=p80,
            level="fitted",
            n=self.stage_rows[state.stage],
            text=f"{self.name}, trained on {self.stage_rows[state.stage]} rows of this stage",
            details=self._details(part, x),
        )

    def _details(self, part: Any, x: np.ndarray) -> dict[str, Any]:
        return {}


class QuantileRegressionModel(_Learned):
    """Linear quantile regression of log remaining time at p50 and p80, one
    per stage."""

    name = "regression"
    ALPHA = 0.001

    def _fit_stage(self, stage: str, rows: list[SnapshotRow]) -> Any:
        from sklearn.linear_model import QuantileRegressor

        design = _Design(self.exclude, stage)
        design.learn([r.features for r in rows])
        x = design.matrix([r.features for r in rows])
        y = np.array([_log_target(r) for r in rows])
        censored = np.array([r.finish_censored for r in rows])
        models = {
            q: _two_pass(
                lambda a, b, q=q: QuantileRegressor(
                    quantile=q, alpha=self.ALPHA, solver="highs"
                ).fit(a, b),
                x,
                y,
                censored,
            )
            for q in (0.5, 0.8)
        }
        return {"design": design, "models": models}

    def _quantiles(self, part: Any, x: np.ndarray) -> tuple[float, float]:
        models = part["models"]
        return (
            float(math.exp(models[0.5].predict(x)[0])),
            float(math.exp(models[0.8].predict(x)[0])),
        )

    def _details(self, part: Any, x: np.ndarray) -> dict[str, Any]:
        model = part["models"][0.5]
        parts: list[dict[str, Any]] = [
            {"feature": n, "contribution": float(c * v)}
            for n, c, v in zip(part["design"].names(), model.coef_, x[0], strict=True)
            if c * v != 0
        ]
        parts.sort(key=lambda p: -abs(p["contribution"]))
        return {
            "intercept_log_seconds": float(model.intercept_),
            "contributions": parts[:6],
            "note": "log-seconds at p50: positive = longer",
        }


class HazardModel(_Learned):
    """Discrete-time hazard, one per stage. Time is working hours since the
    stage began, in bins (``HAZARD_EDGES``); the model is a logistic regression
    of "the task finishes in this bin given it has not yet". A row checkpointed
    after ``e`` hours in the stage enters at the bin containing ``e`` (delayed
    entry), so the time already waited is handled natively: the remaining-time
    distribution of a state is the chain of its hazards from that bin on. A
    task still open contributes the bins it was observed through (no event)."""

    name = "hazard"
    C = 1.0

    @staticmethod
    def _bin(hours: float) -> int:
        return max(i for i, edge in enumerate(HAZARD_EDGES) if edge <= hours)

    def _fit_stage(self, stage: str, rows: list[SnapshotRow]) -> Any:
        from sklearn.linear_model import LogisticRegression

        design = _Design(self.exclude, stage, elapsed=False)
        design.learn([r.features for r in rows])
        base = design.matrix([r.features for r in rows])
        k = len(HAZARD_EDGES)
        xs, ys = [], []
        for features, row in zip(base, rows, strict=True):
            entered = row.features.elapsed_seconds / 3600
            total = entered + (row.finish_remaining_seconds or 0.0) / 3600
            for j in range(self._bin(entered), k):
                start = HAZARD_EDGES[j]
                end = HAZARD_EDGES[j + 1] if j + 1 < k else math.inf
                if row.finish_censored and end > total:
                    break  # observed only through the bins it fully passed
                xs.append([*features, *[1.0 if i == j else 0.0 for i in range(k)]])
                event = start <= total < end
                ys.append(1 if event else 0)
                if event:
                    break
        y = np.array(ys)
        if len(y) == 0 or y.min() == y.max():
            return None
        clf = LogisticRegression(C=self.C, max_iter=500, random_state=0).fit(
            np.array(xs), y
        )
        return {"design": design, "clf": clf}

    def fit(self, data: TrainingData) -> None:
        super().fit(data)
        self.parts = {s: p for s, p in self.parts.items() if p is not None}

    @staticmethod
    def _hazards(part: Any, x: np.ndarray) -> np.ndarray:
        k = len(HAZARD_EDGES)
        rows = np.array(
            [[*x[0], *[1.0 if i == j else 0.0 for i in range(k)]] for j in range(k)]
        )
        return part["clf"].predict_proba(rows)[:, 1]

    def _remaining_hours(
        self, part: Any, x: np.ndarray, elapsed: float, q: float
    ) -> float:
        """The working hours left at quantile ``q``, given ``elapsed`` hours in
        the stage."""
        k = len(HAZARD_EDGES)
        hazards = self._hazards(part, x)
        survival = 1.0
        for j in range(self._bin(elapsed), k):
            start = HAZARD_EDGES[j]
            end = HAZARD_EDGES[j + 1] if j + 1 < k else start * 1.25
            after = survival * (1 - float(hazards[j]))
            if 1 - after >= q:
                frac = (survival - (1 - q)) / max(survival - after, 1e-12)
                return max(start + frac * (end - start) - elapsed, 0.05)
            survival = after
        # The mass did not reach `q` within the horizon: report the horizon.
        return max(HAZARD_EDGES[-1] - elapsed, 0.05)

    def _quantiles(self, part: Any, x: np.ndarray) -> tuple[float, float]:
        raise NotImplementedError  # needs the elapsed time: see predict

    def predict(self, state: SnapshotFeatures) -> Forecast:
        part = self.parts.get(state.stage)
        if part is None:
            rows = self.stage_rows[state.stage]
            return _unknown(
                self.name, rows, f"no history for this stage yet ({rows} rows)"
            )
        x = part["design"].matrix([state])
        elapsed = state.elapsed_seconds / 3600
        grid = tuple(self._remaining_hours(part, x, elapsed, q) * 3600 for q in LEVELS)
        p50, p80 = grid[LEVELS.index(0.5)], grid[LEVELS.index(0.8)]
        hazards = self._hazards(part, x)
        names = part["design"].names()
        coefs = part["clf"].coef_[0][: len(names)]
        top = sorted(zip(names, coefs, strict=True), key=lambda p: -abs(p[1]))[:6]
        return Forecast(
            model=self.name,
            p50_seconds=p50,
            p80_seconds=max(p80, p50),
            level="fitted",
            n=self.stage_rows[state.stage],
            text=f"hazard, trained on {self.stage_rows[state.stage]} rows of this stage",
            grid=grid,
            details={
                "hazard": [
                    {"from_hours": HAZARD_EDGES[j], "hazard": float(h)}
                    for j, h in enumerate(hazards)
                ],
                "coefficients": [
                    {"feature": n, "coefficient": float(c)} for n, c in top
                ],
                "note": "hazard: chance of finishing in the bin given it has not yet",
            },
        )


class BoostedTreesModel(_Learned):
    """Gradient-boosted quantile trees (``HistGradientBoostingRegressor``,
    ``loss="quantile"``) of log remaining time, with the stage, the reviewer
    and the repo as categorical features, so interactions (a reviewer by
    weekday, a size by stage) are found by the trees. Early stopping and a
    minimum leaf size guard against overfitting."""

    name = "trees"

    def _codes(self, states: Sequence[SnapshotFeatures]) -> np.ndarray:
        out = []
        for s in states:
            row = [math.log1p(s.elapsed_seconds / 3600)]
            for name in self.numeric:
                feature = BY_NAME[name]
                value = s.values.get(name)
                if value is None or value == "":
                    row.append(math.nan)
                else:
                    x = float(value)
                    row.append(math.log1p(x) if feature.encoding == "log" else x)
            row.append(self.vocab["stage"].get(s.stage, math.nan))
            for dim in ("reviewer", "repo"):
                row.append(self.vocab[dim].get(str(s.values.get(dim) or ""), math.nan))
            out.append(row)
        return np.array(out, dtype=float)

    def fit(self, data: TrainingData) -> None:
        from sklearn.ensemble import HistGradientBoostingRegressor

        rows = _training_rows(data)
        self.trained_on = len(rows)
        self.stage_rows = Counter(r.features.stage for r in rows)
        self.parts = {}
        if len(rows) < MIN_ROWS:
            return
        states = [r.features for r in rows]
        # A column with no value at all cannot be binned.
        self.numeric = [
            f.name
            for f in REGISTRY
            if f.encoding != "categorical"
            and f.name not in self.exclude
            and any(s.values.get(f.name) not in (None, "") for s in states)
        ]

        def vocabulary(get) -> dict[str, float]:
            return {v: float(i) for i, v in enumerate(sorted({get(s) for s in states}))}

        self.vocab = {
            "stage": vocabulary(lambda s: s.stage),
            "reviewer": vocabulary(lambda s: str(s.values.get("reviewer") or "")),
            "repo": vocabulary(lambda s: str(s.values.get("repo") or "")),
        }
        x = self._codes(states)
        y = np.array([_log_target(r) for r in rows])
        censored = np.array([r.finish_censored for r in rows])
        categorical = [False] * (1 + len(self.numeric)) + [True, True, True]
        self.columns = ["elapsed", *self.numeric, "stage", "reviewer", "repo"]

        def fit_one(q: float):
            def fit(a: np.ndarray, b: np.ndarray):
                # A column with no value at all in this subset cannot be binned.
                a = a.copy()
                a[:, np.isnan(a).all(axis=0)] = 0.0
                return fit_trees(q)(a, b)

            return fit

        def fit_trees(q: float):
            return lambda a, b: HistGradientBoostingRegressor(
                loss="quantile",
                quantile=q,
                max_iter=150,
                learning_rate=0.08,
                max_depth=4,
                min_samples_leaf=max(5, min(20, len(y) // 10)),
                early_stopping=len(y) >= 200,
                validation_fraction=0.2,
                n_iter_no_change=10,
                categorical_features=categorical,
                random_state=0,
            ).fit(a, b)

        self.models = {q: _two_pass(fit_one(q), x, y, censored) for q in (0.5, 0.8)}
        self.importances = self._importances(x[:400], y[:400])
        self.parts = {
            s: True for s, n in self.stage_rows.items() if n >= MIN_STAGE_ROWS
        }

    def _importances(self, x: np.ndarray, y: np.ndarray) -> dict[str, float]:
        """Permutation importance: how much the absolute error of the p50
        model grows when one column is shuffled (fixed seed)."""
        rng = np.random.RandomState(0)
        base = float(np.abs(self.models[0.5].predict(x) - y).mean())
        out = {}
        for column, name in enumerate(self.columns):
            shuffled = x.copy()
            shuffled[:, column] = rng.permutation(shuffled[:, column])
            out[name] = (
                float(np.abs(self.models[0.5].predict(shuffled) - y).mean()) - base
            )
        return out

    def _quantiles(self, part: Any, x: np.ndarray) -> tuple[float, float]:
        return (
            float(math.exp(self.models[0.5].predict(x)[0])),
            float(math.exp(self.models[0.8].predict(x)[0])),
        )

    def predict(self, state: SnapshotFeatures) -> Forecast:
        if state.stage not in self.parts:
            rows = self.stage_rows[state.stage]
            return _unknown(
                self.name, rows, f"no history for this stage yet ({rows} rows)"
            )
        p50, p80 = self._quantiles(None, self._codes([state]))
        top = sorted(self.importances.items(), key=lambda kv: -kv[1])[:6]
        return Forecast(
            model=self.name,
            p50_seconds=p50,
            p80_seconds=max(p80, p50),
            level="fitted",
            n=self.stage_rows[state.stage],
            text=f"trees, trained on {self.trained_on} rows",
            details={
                "importances": [{"feature": k, "importance": v} for k, v in top],
                "note": "permutation importance: the increase in absolute error when the column is shuffled",
            },
        )


CHALLENGERS = {
    "regression": QuantileRegressionModel,
    "hazard": HazardModel,
    "trees": BoostedTreesModel,
}
