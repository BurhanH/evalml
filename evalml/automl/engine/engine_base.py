"""Base class for EvalML engines."""

import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections import OrderedDict

import numpy as np
import pandas as pd
import woodwork as ww

from evalml.automl.utils import (
    get_threshold_tuning_info,
    resplit_training_data,
    tune_binary_threshold,
)
from evalml.exceptions import PipelineScoreError
from evalml.problem_types import (
    ProblemTypes,
    handle_problem_types,
    is_binary,
    is_classification,
)


class EngineComputation(ABC):
    """Wrapper around the result of a (possibly asynchronous) engine computation."""

    @abstractmethod
    def get_result(self):
        """Gets the computation result. Will block until the computation is finished.

        Raises Exception: If computation fails. Returns traceback.
        """

    @abstractmethod
    def done(self):
        """Whether the computation is done."""

    @abstractmethod
    def cancel(self):
        """Cancel the computation."""


class JobLogger:
    """Mimic the behavior of a python logging.Logger but stores all messages rather than actually logging them.

    This is used during engine jobs so that log messages are recorded
    after the job completes. This is desired so that all of the messages
    for a single job are grouped together in the log.
    """

    def __init__(self):
        self.logs = []

    def info(self, msg):
        """Store message at the info level."""
        self.logs.append(("info", msg))

    def debug(self, msg):
        """Store message at the debug level."""
        self.logs.append(("debug", msg))

    def warning(self, msg):
        """Store message at the warning level."""
        self.logs.append(("warning", msg))

    def error(self, msg):
        """Store message at the error level."""
        self.logs.append(("error", msg))

    def write_to_logger(self, logger):
        """Write all the messages to the logger, first in, first out (FIFO) order."""
        logger_method = {
            "info": logger.info,
            "debug": logger.debug,
            "warning": logger.warning,
            "error": logger.warning,
        }
        for level, message in self.logs:
            method = logger_method[level]
            method(message)


class EngineBase(ABC):
    """Base class for EvalML engines."""

    @staticmethod
    def setup_job_log():
        """Set up logger for job."""
        return JobLogger()

    @abstractmethod
    def submit_evaluation_job(
        self,
        automl_config,
        pipeline,
        X,
        y,
        X_holdout=None,
        y_holdout=None,
    ):
        """Submit job for pipeline evaluation during AutoMLSearch."""

    @abstractmethod
    def submit_training_job(
        self,
        automl_config,
        pipeline,
        X,
        y,
        X_holdout=None,
        y_holdout=None,
    ):
        """Submit job for pipeline training."""

    @abstractmethod
    def submit_scoring_job(
        self,
        automl_config,
        pipeline,
        X,
        y,
        objectives,
        X_train=None,
        y_train=None,
    ):
        """Submit job for pipeline scoring."""


def train_pipeline(pipeline, X, y, automl_config, schema=True, get_hashes=False):
    """Train a pipeline and tune the threshold if necessary.

    Args:
        pipeline (PipelineBase): Pipeline to train.
        X (pd.DataFrame): Features to train on.
        y (pd.Series): Target to train on.
        automl_config (AutoMLSearch): The AutoMLSearch object, used to access config and the error callback.
        schema (bool): Whether to use the schemas for X and y. Defaults to True.
        get_hashes (bool): Whether to return the hashes of the data used to train (and potentially threshold). Defaults to False

    Returns:
        pipeline (PipelineBase): A trained pipeline instance.
        hash (optional): The hash of the input data indices, only returned when get_hashes is True.
    """
    X_threshold_tuning = None
    y_threshold_tuning = None
    if automl_config.X_schema and schema:
        X.ww.init(schema=automl_config.X_schema)
    if automl_config.y_schema and schema:
        y.ww.init(schema=automl_config.y_schema)
    (
        threshold_tuning_objective,
        data_needs_resplitting,
    ) = get_threshold_tuning_info(automl_config, pipeline)

    if data_needs_resplitting:
        X, X_threshold_tuning, y, y_threshold_tuning = resplit_training_data(
            pipeline,
            X,
            y,
        )
    cv_pipeline = pipeline.clone()
    cv_pipeline.fit(X, y)
    tune_binary_threshold(
        cv_pipeline,
        threshold_tuning_objective,
        cv_pipeline.problem_type,
        X_threshold_tuning,
        y_threshold_tuning,
        X,
        y,
    )
    if not get_hashes:
        return (cv_pipeline, None)

    X_hash = hash(tuple(X.index))
    return (cv_pipeline, X_hash)


def train_and_score_pipeline(
    pipeline,
    automl_config,
    full_X_train,
    full_y_train,
    logger,
    X_holdout=None,
    y_holdout=None,
):
    """Given a pipeline, config and data, train and score the pipeline and return the CV or TV scores.

    Args:
        pipeline (PipelineBase): The pipeline to score.
        automl_config (AutoMLSearch): The AutoMLSearch object, used to access config and the error callback.
        full_X_train (pd.DataFrame): Training features.
        full_y_train (pd.Series): Training target.
        logger: Logger object to write to.
        X_holdout (pd.DataFrame): Holdout set features.
        y_holdout (pd.DataFrame): Holdout set target.

    Raises:
        Exception: If there are missing target values in the training set after data split.

    Returns:
        tuple of three items: First - A dict containing cv_score_mean, cv_scores, training_time and a cv_data structure with details.
            Second - The pipeline class we trained and scored. Third - the job logger instance with all the recorded messages.
    """

    def _encode_classification_target(y):
        y_mapping = {
            original_target: encoded_target
            for (encoded_target, original_target) in enumerate(
                y.value_counts().index,
            )
        }
        return ww.init_series(y.map(y_mapping))

    def _train_and_score(X_train, X_score, y_train, y_score, fold_num=None):
        fitted_pipeline = pipeline
        prefix = f"Fold {i}" if i is not None else "Full training data pipeline"
        objectives_to_score = [
            automl_config.objective,
        ] + automl_config.additional_objectives
        try:
            logger.debug(f"\t\t\t{prefix}: starting training")
            fitted_pipeline, hashes = train_pipeline(
                pipeline,
                X_train,
                y_train,
                automl_config,
                schema=False,
                get_hashes=True,
            )
            logger.debug(f"\t\t\t{prefix}: finished training")
            if (
                automl_config.optimize_thresholds
                and is_binary(automl_config.problem_type)
                and fitted_pipeline.threshold is not None
            ):
                logger.debug(
                    f"\t\t\t{prefix}: Optimal threshold found ({fitted_pipeline.threshold:.3f})",
                )
            logger.debug(f"\t\t\t{prefix}: Scoring trained pipeline")
            scores = fitted_pipeline.score(
                X_score,
                y_score,
                objectives=objectives_to_score,
                X_train=X_train,
                y_train=y_train,
            )
            logger.debug(
                f"\t\t\t{prefix}: {automl_config.objective.name} score: {scores[automl_config.objective.name]:.3f}",
            )
            score = scores[automl_config.objective.name]
            pipeline_cache[hashes] = fitted_pipeline.component_graph.component_instances
        except Exception as e:
            if automl_config.error_callback is not None:
                automl_config.error_callback(
                    exception=e,
                    traceback=traceback.format_tb(sys.exc_info()[2]),
                    automl=automl_config,
                    fold_num=i,
                    pipeline=pipeline,
                )
            if isinstance(e, PipelineScoreError):
                nan_scores = {objective: np.nan for objective in e.exceptions}
                scores = {**nan_scores, **e.scored_successfully}
                scores = OrderedDict(
                    {
                        o.name: scores[o.name]
                        for o in [automl_config.objective]
                        + automl_config.additional_objectives
                    },
                )
                score = scores[automl_config.objective.name]
            else:
                score = np.nan
                scores = OrderedDict(
                    zip(
                        [n.name for n in automl_config.additional_objectives],
                        [np.nan] * len(automl_config.additional_objectives),
                    ),
                )
        return score, scores, fitted_pipeline

    start = time.time()
    cv_data = []
    use_holdout = X_holdout is not None and y_holdout is not None
    logger.info("\tStarting cross validation")
    # Encode target for classification problems so that we can support float targets. This is okay because we only use split to get the indices to split on
    if is_classification(automl_config.problem_type):
        full_y_train = _encode_classification_target(full_y_train)
        if use_holdout:
            y_holdout = _encode_classification_target(y_holdout)

    pipeline_cache = {}
    stored_pipeline = pipeline

    for i, (train, valid) in enumerate(
        automl_config.data_splitter.split(full_X_train, full_y_train),
    ):
        logger.debug(f"\t\tTraining and scoring on fold {i}")
        X_train, X_valid = full_X_train.ww.iloc[train], full_X_train.ww.iloc[valid]
        y_train, y_valid = full_y_train.ww.iloc[train], full_y_train.ww.iloc[valid]
        if handle_problem_types(automl_config.problem_type) in [
            ProblemTypes.BINARY,
            ProblemTypes.MULTICLASS,
        ]:
            diff_train = set(np.setdiff1d(full_y_train, y_train))
            diff_valid = set(np.setdiff1d(full_y_train, y_valid))
            diff_string = (
                f"Missing target values in the training set after data split: {diff_train}. "
                if diff_train
                else ""
            )
            diff_string += (
                f"Missing target values in the validation set after data split: {diff_valid}."
                if diff_valid
                else ""
            )
            if diff_string:
                raise Exception(diff_string)

        score, scores, stored_pipeline = _train_and_score(
            X_train=X_train,
            X_score=X_valid,
            y_train=y_train,
            y_score=y_valid,
            fold_num=i,
        )

        ordered_scores = OrderedDict()
        ordered_scores.update({automl_config.objective.name: score})
        ordered_scores.update(scores)
        ordered_scores.update({"# Training": y_train.shape[0]})
        ordered_scores.update({"# Validation": y_valid.shape[0]})

        evaluation_entry = {
            "all_objective_scores": ordered_scores,
            "mean_cv_score": score,
            "binary_classification_threshold": None,
        }
        if (
            is_binary(automl_config.problem_type)
            and stored_pipeline is not None
            and stored_pipeline.threshold is not None
        ):
            evaluation_entry[
                "binary_classification_threshold"
            ] = stored_pipeline.threshold
        cv_data.append(evaluation_entry)
    cv_scores = pd.Series([fold["mean_cv_score"] for fold in cv_data])
    cv_score_mean = cv_scores.mean()
    logger.info(
        f"\tFinished cross validation - mean {automl_config.objective.name}: {cv_score_mean:.3f}",
    )

    holdout_score = np.NaN
    holdout_scores = np.NaN
    if use_holdout:
        logger.info("\tStarting holdout set scoring")
        logger.debug("\t\tTraining and scoring entire dataset")
        holdout_score, holdout_scores, stored_pipeline = _train_and_score(
            X_train=full_X_train,
            X_score=X_holdout,
            y_train=full_y_train,
            y_score=y_holdout,
        )
        logger.info(
            f"\tFinished holdout set scoring - {automl_config.objective.name}: {holdout_score:.3f}",
        )

    training_time = time.time() - start
    return {
        "scores": {
            "cv_data": cv_data,
            "training_time": training_time,
            "cv_scores": cv_scores,
            "cv_score_mean": cv_score_mean,
            "holdout_score": None if not use_holdout else holdout_score,
            "holdout_scores": None if not use_holdout else holdout_scores,
        },
        "cached_data": pipeline_cache,
        "pipeline": stored_pipeline,
        "logger": logger,
    }


def evaluate_pipeline(
    pipeline,
    automl_config,
    X,
    y,
    logger,
    X_holdout=None,
    y_holdout=None,
):
    """Function submitted to the submit_evaluation_job engine method.

    Args:
        pipeline (PipelineBase): The pipeline to score.
        automl_config (AutoMLConfig): The AutoMLSearch object, used to access config and the error callback.
        X (pd.DataFrame): Training features.
        y (pd.Series): Training target.
        logger: Logger object to write to.
        X_holdout (pd.DataFrame): Holdout set features.
        y_holdout (pd.DataFrame): Holdout set target.

    Returns:
        tuple of three items: First - A dict containing cv_score_mean, cv_scores, training_time and a cv_data structure with details.
            Second - The pipeline class we trained and scored. Third - the job logger instance with all the recorded messages.
    """
    logger.info(f"{pipeline.name}:")

    X.ww.init(schema=automl_config.X_schema)
    y.ww.init(schema=automl_config.y_schema)

    return train_and_score_pipeline(
        pipeline,
        automl_config=automl_config,
        full_X_train=X,
        full_y_train=y,
        logger=logger,
        X_holdout=X_holdout,
        y_holdout=y_holdout,
    )


def score_pipeline(
    pipeline,
    X,
    y,
    objectives,
    X_train=None,
    y_train=None,
    X_schema=None,
    y_schema=None,
):
    """Wrap around pipeline.score method to make it easy to score pipelines with dask.

    Args:
        pipeline (PipelineBase): The pipeline to score.
        X (pd.DataFrame): Features to score on.
        y (pd.Series): Target used to calculate scores.
        objectives (list[ObjectiveBase]): List of objectives to score on.
        X_train (pd.DataFrame): Training features. Used for feature engineering in time series.
        y_train (pd.Series): Training target. Used for feature engineering in time series.
        X_schema (ww.TableSchema): Schema for features. Defaults to None.
        y_schema (ww.ColumnSchema): Schema for columns. Defaults to None.

    Returns:
        dict: Dictionary object containing pipeline scores.
    """
    if X_schema:
        X.ww.init(schema=X_schema)
    if y_schema:
        y.ww.init(schema=y_schema)
    return pipeline.score(X, y, objectives, X_train=X_train, y_train=y_train)
