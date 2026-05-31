"""
Unit tests for core services.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.models.cir import ActionType, CIRBlock, CIRAction
from app.models.step_repair import (
    StepRepairRequest,
    ErrorClassification,
    ErrorDetails,
    Artifacts,
)
from app.models.extraction import ExtractedLocator, ExtractedValue
from app.core.utils import (
    extract_quoted,
    extract_code_block,
    sanitize_selector,
    ExecutionStatus,
    RepairOutcome,
    ExtractionResult,
)


# =============================================================================
# Text Utility Tests
# =============================================================================

class TestExtractQuoted:
    """Tests for extract_quoted utility."""
    
    def test_extracts_double_quoted_string(self):
        result = extract_quoted('Click on "Submit Button"')
        assert result == "Submit Button"
    
    def test_extracts_first_quoted_string(self):
        result = extract_quoted('Click "First" then "Second"')
        assert result == "First"
    
    def test_returns_none_for_no_quotes(self):
        result = extract_quoted("No quotes here")
        assert result is None
    
    def test_handles_empty_quotes(self):
        result = extract_quoted('Empty ""')
        assert result == ""
    
    def test_handles_single_quotes_fallback(self):
        result = extract_quoted("Click on 'Submit'")
        assert result == "Submit"


class TestExtractCodeBlock:
    """Tests for extract_code_block utility."""
    
    def test_extracts_python_code_block(self):
        text = '''Here is the code:
```python
await page.click("#submit")
```
Done'''
        result = extract_code_block(text)
        assert result == 'await page.click("#submit")'
    
    def test_extracts_generic_code_block(self):
        text = '''```
await page.fill("#input", "test")
```'''
        result = extract_code_block(text)
        assert result == 'await page.fill("#input", "test")'
    
    def test_returns_original_if_no_block(self):
        text = 'await page.click("#btn")'
        result = extract_code_block(text)
        assert result == text
    
    def test_handles_multiline_code(self):
        text = '''```python
line1
line2
line3
```'''
        result = extract_code_block(text)
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result


class TestSanitizeSelector:
    """Tests for sanitize_selector utility."""
    
    def test_removes_javascript_injection(self):
        selector = 'button[onclick="javascript:alert(1)"]'
        result = sanitize_selector(selector)
        assert "javascript:" not in result
    
    def test_preserves_valid_selector(self):
        selector = 'button.submit-btn[data-testid="submit"]'
        result = sanitize_selector(selector)
        assert result == selector
    
    def test_handles_text_selector(self):
        selector = 'text="Submit Form"'
        result = sanitize_selector(selector)
        assert result == selector


# =============================================================================
# Enum Tests
# =============================================================================

class TestExecutionStatus:
    """Tests for ExecutionStatus enum."""
    
    def test_status_values(self):
        assert ExecutionStatus.PASSED.value == "passed"
        assert ExecutionStatus.FAILED.value == "failed"
        assert ExecutionStatus.TIMEOUT.value == "timeout"
        assert ExecutionStatus.SKIPPED.value == "skipped"
    
    def test_is_terminal(self):
        assert ExecutionStatus.PASSED.is_terminal() is True
        assert ExecutionStatus.FAILED.is_terminal() is True
        assert ExecutionStatus.RUNNING.is_terminal() is False
        assert ExecutionStatus.PENDING.is_terminal() is False


class TestRepairOutcome:
    """Tests for RepairOutcome enum."""
    
    def test_outcome_values(self):
        assert RepairOutcome.SUCCESS.value == "success"
        assert RepairOutcome.PARTIAL.value == "partial"
        assert RepairOutcome.FAILURE.value == "failure"
    
    def test_is_success(self):
        assert RepairOutcome.SUCCESS.is_success() is True
        assert RepairOutcome.PARTIAL.is_success() is False
        assert RepairOutcome.FAILURE.is_success() is False


# =============================================================================
# Extraction Result Tests
# =============================================================================

class TestExtractionResult:
    """Tests for ExtractionResult dataclass."""
    
    def test_successful_extraction(self):
        locator = ExtractedLocator(strategy="css", value="#submit")
        result = ExtractionResult(
            locator=locator,
            value=None,
            confidence=0.95,
            source="llm"
        )
        assert result.is_successful() is True
        assert result.confidence == 0.95
    
    def test_failed_extraction(self):
        result = ExtractionResult(
            locator=None,
            value=None,
            confidence=0.0,
            source="fallback"
        )
        assert result.is_successful() is False
    
    def test_with_value(self):
        locator = ExtractedLocator(strategy="css", value="#input")
        value = ExtractedValue(text="test input", is_sensitive=False)
        result = ExtractionResult(
            locator=locator,
            value=value,
            confidence=0.9,
            source="llm"
        )
        assert result.locator is not None
        assert result.value is not None
        assert result.value.text == "test input"


# =============================================================================
# CIR Model Tests
# =============================================================================

class TestCIRBlock:
    """Tests for CIR block model."""
    
    def test_creates_valid_block(self):
        action = CIRAction(
            action_type=ActionType.click,
            locator='button:has-text("Submit")',
            description="Click submit button"
        )
        block = CIRBlock(
            step_id="test__step_1",
            intent="Submit the form",
            actions=[action]
        )
        assert block.step_id == "test__step_1"
        assert len(block.actions) == 1
        assert block.actions[0].action_type == ActionType.click
    
    def test_block_with_multiple_actions(self):
        actions = [
            CIRAction(
                action_type=ActionType.click,
                locator="#field",
                description="Focus field"
            ),
            CIRAction(
                action_type=ActionType.type,
                locator="#field",
                value="test",
                description="Type value"
            ),
        ]
        block = CIRBlock(
            step_id="test__step_2",
            intent="Fill form field",
            actions=actions
        )
        assert len(block.actions) == 2


class TestCIRAction:
    """Tests for CIR action model."""
    
    def test_click_action(self):
        action = CIRAction(
            action_type=ActionType.click,
            locator='text="Login"',
            description="Click login"
        )
        assert action.action_type == ActionType.click
        assert action.value is None
    
    def test_type_action(self):
        action = CIRAction(
            action_type=ActionType.type,
            locator="#username",
            value="testuser",
            description="Enter username"
        )
        assert action.action_type == ActionType.type
        assert action.value == "testuser"
    
    def test_select_action(self):
        action = CIRAction(
            action_type=ActionType.select,
            locator="#country",
            value="US",
            description="Select country"
        )
        assert action.action_type == ActionType.select


# =============================================================================
# Step Repair Request Tests
# =============================================================================

class TestStepRepairRequest:
    """Tests for step repair request model."""
    
    def test_valid_request(self):
        request = StepRepairRequest(
            step_id="test_login__step_1",
            step_intent="Click the login button",
            original_code='await page.click("#login")',
            error_classification=ErrorClassification(
                type="LOCATOR_NOT_FOUND",
                subtype="timeout"
            ),
            error_details=ErrorDetails(
                message="Timeout 5000ms exceeded",
                stack_trace="at click (/test.py:10)"
            ),
        )
        assert request.step_id == "test_login__step_1"
        assert request.error_classification.type == "LOCATOR_NOT_FOUND"
    
    def test_request_with_artifacts(self):
        request = StepRepairRequest(
            step_id="test__step_1",
            step_intent="Fill username",
            original_code='await page.fill("#user", "test")',
            error_classification=ErrorClassification(type="ELEMENT_NOT_VISIBLE"),
            error_details=ErrorDetails(message="Element not visible"),
            artifacts=Artifacts(
                dom_snapshot="<input id='username' type='text'>",
                page_url="http://localhost:3000",
            ),
        )
        assert request.artifacts is not None
        assert request.artifacts.dom_snapshot is not None


# =============================================================================
# Error Classification Tests
# =============================================================================

class TestErrorClassification:
    """Tests for error classification model."""
    
    def test_locator_not_found(self):
        classification = ErrorClassification(
            type="LOCATOR_NOT_FOUND",
            subtype="timeout",
            confidence=0.95
        )
        assert classification.type == "LOCATOR_NOT_FOUND"
        assert classification.confidence == 0.95
    
    def test_assertion_failed(self):
        classification = ErrorClassification(
            type="ASSERTION_FAILED",
            subtype="text_mismatch"
        )
        assert classification.type == "ASSERTION_FAILED"


# =============================================================================
# Integration Style Tests (with mocks)
# =============================================================================

class TestCIRBuilderIntegration:
    """Integration-style tests for CIR builder with mocked LLM."""
    
    @pytest.mark.asyncio
    async def test_builds_click_cir(self, mock_llm_executor, sample_repair_request):
        """Test building CIR for click action."""
        from app.services.cir_builder import CIRBuilder
        
        # Configure mock
        mock_llm_executor.run_classifier.return_value = "click"
        mock_llm_executor.run_extractor.return_value = 'click:text("Login")'
        
        builder = CIRBuilder()
        builder.classifier.llm = mock_llm_executor
        builder.click_extractor.llm = mock_llm_executor
        
        # Build CIR
        block, context = await builder.build(request=sample_repair_request)
        
        assert block is not None
        assert len(block.actions) >= 1
        assert block.actions[0].action_type == ActionType.click
    
    @pytest.mark.asyncio
    async def test_builds_type_cir(self, mock_llm_executor):
        """Test building CIR for type action."""
        from app.services.cir_builder import CIRBuilder
        
        request = StepRepairRequest(
            step_id="test__step_1",
            step_intent="Type username into the field",
            original_code='await page.fill("#user", "admin")',
            error_classification=ErrorClassification(type="LOCATOR_NOT_FOUND"),
            error_details=ErrorDetails(message="Timeout"),
        )
        
        mock_llm_executor.run_classifier.return_value = "type"
        mock_llm_executor.run_extractor.return_value = 'type:placeholder("username") value("username")'
        
        builder = CIRBuilder()
        builder.classifier.llm = mock_llm_executor
        builder.type_extractor.llm = mock_llm_executor
        
        block, context = await builder.build(request=request)
        
        assert block is not None
        # Should have focus + type actions
        assert any(a.action_type == ActionType.type for a in block.actions)


class TestStepCodeGeneratorIntegration:
    """Integration-style tests for code generator."""
    
    def test_generates_click_code(self, mock_llm_executor):
        """Test generating Playwright code for click action."""
        from app.services.generator import StepCodeGenerator
        
        action = CIRAction(
            action_type=ActionType.click,
            locator='button:has-text("Submit")',
            description="Click submit"
        )
        
        generator = StepCodeGenerator()
        code_lines = generator.generate(action)
        code = "\n".join(code_lines)
        
        assert code is not None
        assert "click" in code.lower() or "submit" in code.lower()
    
    def test_generates_type_code(self, mock_llm_executor):
        """Test generating Playwright code for type action."""
        from app.services.generator import StepCodeGenerator
        
        action = CIRAction(
            action_type=ActionType.type,
            locator="#username",
            value="testuser",
            description="Type username"
        )
        
        generator = StepCodeGenerator()
        code_lines = generator.generate(action)
        code = "\n".join(code_lines)
        
        assert code is not None
        assert "fill" in code.lower() or "type" in code.lower()


class TestStepVerifierIntegration:
    """Integration-style tests for step verifier."""
    
    @pytest.mark.asyncio
    async def test_verifies_valid_code(self, mock_llm_executor):
        """Test verification of syntactically correct code."""
        from app.services.step_verifier import StepVerifier
        
        mock_llm_executor.run_verifier.return_value = "PASS"
        
        verifier = StepVerifier()
        verifier.llm = mock_llm_executor
        
        code = 'await page.click("button#submit")'
        intent = "Click submit button"
        
        result = await verifier.verify(code, intent)
        
        assert result.passed is True
    
    @pytest.mark.asyncio
    async def test_rejects_invalid_code(self, mock_llm_executor):
        """Test rejection of invalid code."""
        from app.services.step_verifier import StepVerifier
        
        mock_llm_executor.run_verifier.return_value = "FAIL: syntax error"
        
        verifier = StepVerifier()
        verifier.llm = mock_llm_executor
        
        code = 'await page.click('  # Invalid syntax
        intent = "Click button"
        
        result = await verifier.verify(code, intent)
        
        assert result.passed is False


# =============================================================================
# Safety Module Tests
# =============================================================================

class TestCircuitBreaker:
    """Tests for circuit breaker pattern."""
    
    @pytest.mark.asyncio
    async def test_circuit_opens_after_failures(self):
        """Test that circuit opens after threshold failures."""
        from app.core.resilience import CircuitBreaker
        
        breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=1.0,
            half_open_max=1
        )
        
        # Record failures
        for _ in range(3):
            breaker.record_failure()
        
        assert breaker.is_open() is True
    
    @pytest.mark.asyncio
    async def test_circuit_allows_when_closed(self):
        """Test that circuit allows calls when closed."""
        from app.core.resilience import CircuitBreaker
        
        breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=1.0,
            half_open_max=1
        )
        
        assert breaker.is_open() is False
        breaker.record_success()
        assert breaker.is_open() is False


class TestBackoffPolicy:
    """Tests for backoff policy."""
    
    def test_exponential_backoff(self):
        """Test exponential backoff calculation."""
        from app.core.resilience import BackoffPolicy
        
        policy = BackoffPolicy(
            base_delay=1.0,
            max_delay=60.0,
            exponential_base=2.0,
            jitter=False
        )
        
        assert policy.get_delay(attempt=0) == 1.0
        assert policy.get_delay(attempt=1) == 2.0
        assert policy.get_delay(attempt=2) == 4.0
    
    def test_max_delay_cap(self):
        """Test that delay is capped at max."""
        from app.core.resilience import BackoffPolicy
        
        policy = BackoffPolicy(
            base_delay=1.0,
            max_delay=10.0,
            exponential_base=2.0,
            jitter=False
        )
        
        # 2^10 = 1024, should be capped at 10
        assert policy.get_delay(attempt=10) == 10.0


class TestFailureFingerprint:
    """Tests for failure fingerprinting."""
    
    def test_same_error_same_fingerprint(self):
        """Test that identical errors produce same fingerprint."""
        from app.core.utils import FailureFingerprint
        
        fp1 = FailureFingerprint.create(
            step_id="test__step_1",
            error_type="LOCATOR_NOT_FOUND",
            error_message="Timeout waiting for selector"
        )
        
        fp2 = FailureFingerprint.create(
            step_id="test__step_1",
            error_type="LOCATOR_NOT_FOUND",
            error_message="Timeout waiting for selector"
        )
        
        assert fp1 == fp2
    
    def test_different_errors_different_fingerprint(self):
        """Test that different errors produce different fingerprints."""
        from app.core.utils import FailureFingerprint
        
        fp1 = FailureFingerprint.create(
            step_id="test__step_1",
            error_type="LOCATOR_NOT_FOUND",
            error_message="Error 1"
        )
        
        fp2 = FailureFingerprint.create(
            step_id="test__step_1",
            error_type="ASSERTION_FAILED",
            error_message="Error 2"
        )
        
        assert fp1 != fp2
