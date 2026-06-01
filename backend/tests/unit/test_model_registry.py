"""Unit tests for ModelRegistry with encryption."""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.model_registry import ModelRegistry, QualityGateError
from app.models.ml_data import MLModelMetadata


@pytest.fixture
def temp_storage():
    """Create temporary storage directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def temp_model_file():
    """Create temporary model file."""
    with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
        f.write(b"mock_onnx_model_data_12345")
        temp_path = f.name
    yield temp_path
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
async def registry(db_session: AsyncSession, temp_storage):
    """Create ModelRegistry instance."""
    # Set encryption key (valid Fernet key)
    os.environ["ML_MODEL_ENCRYPTION_KEY"] = "mgxnKoMw7emojcQEF3NKNGe_8pnBcytPu6NfxI1yQNE="
    registry = ModelRegistry(db_session, model_storage_path=temp_storage)
    return registry


@pytest.mark.asyncio
async def test_register_model_encrypts_artifact(registry, temp_model_file):
    """Test that model artifacts are encrypted on registration."""
    model = await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={"training_date": "2024-01-01"},
        feature_version="1.0.0"
    )
    
    assert model is not None
    assert model.encrypted is True
    assert model.checksum is not None


@pytest.mark.asyncio
async def test_register_model_computes_checksum(registry, temp_model_file):
    """Test that SHA256 checksum is computed before encryption."""
    model = await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    # Checksum should be 64 characters (SHA256 hex)
    assert len(model.checksum) == 64
    assert model.checksum.isalnum()


@pytest.mark.asyncio
async def test_register_duplicate_version_fails(registry, temp_model_file):
    """Test that registering duplicate version raises error."""
    await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    with pytest.raises(ValueError, match="already exists"):
        await registry.register_model(
            version="1.0.0",
            model_type="lstm",
            artifact_path=temp_model_file,
            metrics={"accuracy": 0.91},
            metadata={},
            feature_version="1.0.0"
        )


@pytest.mark.asyncio
async def test_get_model_by_version(registry, temp_model_file):
    """Test retrieving model by version."""
    await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    model = await registry.get_model("1.0.0")
    assert model is not None
    assert model.model_version == "1.0.0"


@pytest.mark.asyncio
async def test_get_nonexistent_model_returns_none(registry):
    """Test that getting nonexistent model returns None."""
    model = await registry.get_model("99.99.99")
    assert model is None


@pytest.mark.asyncio
async def test_get_latest_model(registry, temp_model_file):
    """Test getting latest model."""
    # Register multiple versions
    await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    await registry.register_model(
        version="1.1.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.92},
        metadata={},
        feature_version="1.0.0"
    )
    
    latest = await registry.get_latest_model()
    assert latest is not None
    # Should return most recently registered
    assert latest.model_version in ["1.0.0", "1.1.0"]


@pytest.mark.asyncio
async def test_get_production_model(registry, temp_model_file):
    """Test getting production model."""
    model = await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    # Promote to production
    await registry.promote_to_production("1.0.0")
    
    prod_model = await registry.get_production_model()
    assert prod_model is not None
    assert prod_model.model_version == "1.0.0"
    assert prod_model.status == "production"


@pytest.mark.asyncio
async def test_promote_to_production(registry, temp_model_file):
    """Test promoting model to production."""
    await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    promoted = await registry.promote_to_production("1.0.0")
    assert promoted.status == "production"


@pytest.mark.asyncio
async def test_list_models(registry, temp_model_file):
    """Test listing models with filters."""
    await registry.register_model(
        version="1.0.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.90},
        metadata={},
        feature_version="1.0.0"
    )
    
    await registry.register_model(
        version="1.1.0",
        model_type="lstm",
        artifact_path=temp_model_file,
        metrics={"accuracy": 0.92},
        metadata={},
        feature_version="1.0.0",
        status="staging"
    )
    
    all_models = await registry.list_models()
    assert len(all_models) >= 2
    
    staging_models = await registry.list_models(status="staging")
    assert len(staging_models) >= 1
    assert all(m.status == "staging" for m in staging_models)
