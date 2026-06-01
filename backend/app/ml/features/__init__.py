"""
ML Features Package

Feature engineering modules for ML model training.
Complete pipeline from data to ML-ready features.
"""

from .ohlcv_features import (
    compute_all_technical_features,
    get_feature_names as get_technical_feature_names
)
from .sentiment_features import (
    compute_all_sentiment_features,
    get_sentiment_feature_names,
    merge_sentiment_with_ohlcv
)
from .feature_pipeline import (
    compute_features_for_symbol,
    compute_features_batch,
    normalize_features,
    create_sequences,
    prepare_training_data,
    get_all_feature_names
)
from .feature_store import (
    save_features_to_db,
    load_features_from_db,
    cache_features_to_redis,
    get_cached_features,
    batch_save_features
)
from .target_generator import (
    create_target_variable,
    add_target_to_features,
    create_targets_batch,
    create_targets_from_db,
    get_class_weights,
    split_features_targets
)
from .validation import (
    validate_features,
    validate_feature_count,
    validate_sample_count,
    get_feature_statistics
)
from .symbol_selector import (
    get_top_liquid_symbols,
    analyze_symbol_data_quality,
    batch_analyze_symbols
)
from .engine import FeatureEngine, get_feature_engine

__all__ = [
    # Technical features
    'compute_all_technical_features',
    'get_technical_feature_names',
    # Sentiment features
    'compute_all_sentiment_features',
    'get_sentiment_feature_names',
    'merge_sentiment_with_ohlcv',
    # Feature pipeline
    'compute_features_for_symbol',
    'compute_features_batch',
    'normalize_features',
    'create_sequences',
    'prepare_training_data',
    'get_all_feature_names',
    # Feature store
    'save_features_to_db',
    'load_features_from_db',
    'cache_features_to_redis',
    'get_cached_features',
    'batch_save_features',
    # Target generation
    'create_target_variable',
    'add_target_to_features',
    'create_targets_batch',
    'create_targets_from_db',
    'get_class_weights',
    'split_features_targets',
    # Validation
    'validate_features',
    'validate_feature_count',
    'validate_sample_count',
    'get_feature_statistics',
    # Symbol selection
    'get_top_liquid_symbols',
    'analyze_symbol_data_quality',
    'batch_analyze_symbols',
    # Feature engine
    'FeatureEngine',
    'get_feature_engine',
]
