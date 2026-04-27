"""
Custom JSON Formatter with Request Context
===========================================
Extends pythonjsonlogger to include request_id, correlation_id, user_id, and other contextual fields.
"""
from datetime import datetime, timezone
from pythonjsonlogger import jsonlogger


class ContextualJsonFormatter(jsonlogger.JsonFormatter):
    """
    JSON formatter that automatically includes request context from contextvars.
    
    Fields:
    - timestamp: ISO 8601 with timezone
    - level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - logger: Logger name
    - message: Log message
    - request_id: Unique request identifier (from middleware)
    - correlation_id: Cross-service correlation ID (from middleware)
    - user_id: Authenticated user ID (from JWT token)
    - endpoint: API endpoint path
    - method: HTTP method
    - duration_ms: Request duration in milliseconds
    - Any extra fields passed via logger.info(..., extra={})
    """
    
    def add_fields(self, log_record, record, message_dict):
        """Add custom fields to log record."""
        super().add_fields(log_record, record, message_dict)
        
        # Add ISO 8601 timestamp with timezone
        log_record['timestamp'] = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat()
        
        # Add standard fields
        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        
        # Add contextual fields from contextvars (set by middleware)
        from app.core.request_context import get_request_context
        context = get_request_context()
        
        if context:
            if context.get('request_id'):
                log_record['request_id'] = context['request_id']
            if context.get('correlation_id'):
                log_record['correlation_id'] = context['correlation_id']
            if context.get('user_id'):
                log_record['user_id'] = context['user_id']
            if context.get('endpoint'):
                log_record['endpoint'] = context['endpoint']
            if context.get('method'):
                log_record['method'] = context['method']
        
        # Include any extra fields passed via logger.info(..., extra={})
        # These override context fields if present
        for key, value in message_dict.items():
            if key not in log_record:
                log_record[key] = value
