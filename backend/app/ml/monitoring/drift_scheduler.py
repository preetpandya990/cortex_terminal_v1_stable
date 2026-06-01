"""
Cortex AI — Drift Monitoring Scheduler
========================================
Schedules daily drift detection and sends alerts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.monitoring.drift_detector import DriftDetector
from app.models.ml_data import MLDriftMetric, MLModelMetadata


logger = logging.getLogger(__name__)


class DriftMonitoringScheduler:
    """
    Schedules and executes drift detection for active models.
    
    Runs daily at market close and sends alerts when drift is detected.
    """
    
    def __init__(
        self,
        db_url: Optional[str] = None,
        market_close_time: time = time(16, 0),  # 4:00 PM
        slack_webhook_url: Optional[str] = None,
        alert_email: Optional[str] = None,
    ):
        """
        Initialize drift monitoring scheduler.
        
        Args:
            db_url: Database connection URL (uses settings if not provided)
            market_close_time: Time to run drift detection daily
            slack_webhook_url: Slack webhook for alerts
            alert_email: Email address for alerts
        """
        settings = get_settings()
        self.db_url = db_url or str(settings.DATABASE_URL)
        self.market_close_time = market_close_time
        self.slack_webhook_url = slack_webhook_url
        self.alert_email = alert_email
        
        # Create async engine and session factory
        self.engine = create_async_engine(self.db_url, echo=False)
        self.async_session_factory = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        self._running = False
        
    async def start(self):
        """Start the drift monitoring scheduler."""
        self._running = True
        logger.info("Drift monitoring scheduler started")
        
        while self._running:
            try:
                # Calculate time until next run
                now = datetime.now()
                next_run = datetime.combine(now.date(), self.market_close_time)
                
                # If market close already passed today, schedule for tomorrow
                if now.time() > self.market_close_time:
                    next_run = datetime.combine(
                        now.date() + timedelta(days=1),
                        self.market_close_time
                    )
                
                wait_seconds = (next_run - now).total_seconds()
                logger.info(f"Next drift detection scheduled at {next_run}")
                
                # Wait until scheduled time
                await asyncio.sleep(wait_seconds)
                
                # Run drift detection
                await self.run_drift_detection()
                
            except Exception as e:
                logger.error(f"Error in drift monitoring scheduler: {e}", exc_info=True)
                # Wait 1 hour before retrying on error
                await asyncio.sleep(3600)
    
    def stop(self):
        """Stop the drift monitoring scheduler."""
        self._running = False
        logger.info("Drift monitoring scheduler stopped")
    
    async def run_drift_detection(self):
        """
        Run drift detection for all active models.
        
        Fetches active models, runs drift detection, and sends alerts.
        """
        logger.info("Starting scheduled drift detection")
        
        async with self.async_session_factory() as session:
            try:
                # Get all active models
                from sqlalchemy import select
                result = await session.execute(
                    select(MLModelMetadata)
                    .where(MLModelMetadata.is_active == True)
                )
                active_models = list(result.scalars().all())
                
                logger.info(f"Found {len(active_models)} active models")
                
                # Run drift detection for each model
                drift_results = []
                for model in active_models:
                    try:
                        detector = DriftDetector(
                            model_id=model.model_id,
                            db_session=session,
                            drift_threshold=2.0,
                            lookback_days=7,
                        )
                        
                        drift_metric = await detector.detect_drift()
                        
                        if drift_metric and drift_metric.drift_detected:
                            drift_results.append(drift_metric)
                            logger.warning(
                                f"Drift detected for model {model.model_id}: "
                                f"type={drift_metric.drift_type}"
                            )
                        else:
                            logger.info(f"No drift detected for model {model.model_id}")
                            
                    except Exception as e:
                        logger.error(
                            f"Error detecting drift for model {model.model_id}: {e}",
                            exc_info=True
                        )
                
                # Send alerts for detected drift
                if drift_results:
                    await self.send_alerts(drift_results, session)
                
                logger.info(
                    f"Drift detection complete: {len(drift_results)} models with drift"
                )
                
            except Exception as e:
                logger.error(f"Error in drift detection run: {e}", exc_info=True)
    
    async def send_alerts(
        self,
        drift_metrics: list[MLDriftMetric],
        session: AsyncSession,
    ):
        """
        Send alerts for detected drift.
        
        Args:
            drift_metrics: List of drift metrics with detected drift
            session: Database session for updating alert status
        """
        logger.info(f"Sending alerts for {len(drift_metrics)} drift detections")
        
        for metric in drift_metrics:
            try:
                # Send Slack alert
                if self.slack_webhook_url:
                    await self._send_slack_alert(metric)
                
                # Send email alert
                if self.alert_email:
                    await self._send_email_alert(metric)
                
                # Update alert status
                metric.alert_sent = True
                metric.alert_sent_at = datetime.utcnow()
                await session.commit()
                
                logger.info(f"Alert sent for model {metric.model_id}")
                
            except Exception as e:
                logger.error(
                    f"Error sending alert for model {metric.model_id}: {e}",
                    exc_info=True
                )
    
    async def _send_slack_alert(self, metric: MLDriftMetric):
        """Send Slack notification for drift detection."""
        import aiohttp
        
        message = self._format_alert_message(metric)
        
        payload = {
            "text": f"🚨 ML Model Drift Detected",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🚨 ML Model Drift Detected"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Model ID:*\n{metric.model_id}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Drift Type:*\n{metric.drift_type}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Z-Score:*\n{metric.prediction_z_score:.2f}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Timestamp:*\n{metric.timestamp.isoformat()}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Alerts:*\n" + "\n".join(f"• {alert}" for alert in metric.alerts)
                    }
                }
            ]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.slack_webhook_url, json=payload) as response:
                if response.status != 200:
                    logger.error(f"Slack alert failed: {response.status}")
                else:
                    logger.info("Slack alert sent successfully")
    
    async def _send_email_alert(self, metric: MLDriftMetric):
        """Send email notification for drift detection."""
        # This is a placeholder - implement with your email service
        # (e.g., SendGrid, AWS SES, SMTP)
        
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        settings = get_settings()
        
        message = MIMEMultipart("alternative")
        message["Subject"] = f"ML Model Drift Detected - {metric.model_id}"
        message["From"] = settings.ALERT_EMAIL_FROM if hasattr(settings, "ALERT_EMAIL_FROM") else "alerts@cortex-ai.com"
        message["To"] = self.alert_email
        
        # Create email body
        text_content = self._format_alert_message(metric)
        html_content = self._format_alert_html(metric)
        
        part1 = MIMEText(text_content, "plain")
        part2 = MIMEText(html_content, "html")
        
        message.attach(part1)
        message.attach(part2)
        
        try:
            # Send email (configure SMTP settings in your config)
            smtp_host = getattr(settings, "SMTP_HOST", "localhost")
            smtp_port = getattr(settings, "SMTP_PORT", 587)
            
            await aiosmtplib.send(
                message,
                hostname=smtp_host,
                port=smtp_port,
                start_tls=True,
            )
            logger.info("Email alert sent successfully")
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")
    
    def _format_alert_message(self, metric: MLDriftMetric) -> str:
        """Format drift metric as text message."""
        lines = [
            f"ML Model Drift Detected",
            f"",
            f"Model ID: {metric.model_id}",
            f"Timestamp: {metric.timestamp.isoformat()}",
            f"Drift Type: {metric.drift_type}",
            f"",
            f"Prediction Metrics:",
            f"  Z-Score: {metric.prediction_z_score:.4f}",
            f"  KS Statistic: {metric.prediction_ks_statistic:.4f}",
            f"  Current Mean: {metric.current_mean:.4f}",
            f"  Historical Mean: {metric.historical_mean:.4f}",
            f"",
        ]
        
        if metric.drifted_features:
            lines.append(f"Drifted Features ({len(metric.drifted_features)}):")
            for feature in metric.drifted_features[:10]:
                score = metric.feature_drift_scores.get(feature, 0)
                lines.append(f"  - {feature}: {score:.4f}")
            if len(metric.drifted_features) > 10:
                lines.append(f"  ... and {len(metric.drifted_features) - 10} more")
            lines.append("")
        
        if metric.alerts:
            lines.append("Alerts:")
            for alert in metric.alerts:
                lines.append(f"  - {alert}")
        
        return "\n".join(lines)
    
    def _format_alert_html(self, metric: MLDriftMetric) -> str:
        """Format drift metric as HTML email."""
        html = f"""
        <html>
        <body>
            <h2>🚨 ML Model Drift Detected</h2>
            <table style="border-collapse: collapse; width: 100%;">
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>Model ID</strong></td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{metric.model_id}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>Timestamp</strong></td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{metric.timestamp.isoformat()}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>Drift Type</strong></td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{metric.drift_type}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>Z-Score</strong></td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{metric.prediction_z_score:.4f}</td>
                </tr>
            </table>
            
            <h3>Alerts</h3>
            <ul>
        """
        
        for alert in metric.alerts:
            html += f"<li>{alert}</li>"
        
        html += """
            </ul>
        </body>
        </html>
        """
        
        return html


# Convenience function to run scheduler
async def run_drift_monitoring(
    slack_webhook_url: Optional[str] = None,
    alert_email: Optional[str] = None,
):
    """
    Run drift monitoring scheduler.
    
    Args:
        slack_webhook_url: Slack webhook URL for alerts
        alert_email: Email address for alerts
    """
    scheduler = DriftMonitoringScheduler(
        slack_webhook_url=slack_webhook_url,
        alert_email=alert_email,
    )
    
    try:
        await scheduler.start()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        scheduler.stop()


if __name__ == "__main__":
    # Run scheduler as standalone script
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    slack_url = sys.argv[1] if len(sys.argv) > 1 else None
    email = sys.argv[2] if len(sys.argv) > 2 else None
    
    asyncio.run(run_drift_monitoring(slack_url, email))



async def drift_detection_loop(session_factory: async_sessionmaker) -> None:
    """
    Drift detection background loop.
    
    Monitors ML model drift and triggers state transitions when thresholds exceeded.
    Runs until cancelled via asyncio.CancelledError.
    
    Args:
        session_factory: SQLAlchemy async session factory
    """
    from sqlalchemy import select
    from app.ai.fusion.models import AIMLModel
    from app.ai.governance.drift_detector import DriftDetector as AIDriftDetector
    from app.core.redis import get_redis, PubSubClient
    
    logger.info("Drift detection loop started")
    settings = get_settings()
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    redis = await get_redis()
                    pubsub = PubSubClient(redis)
                    
                    # Query active AI models
                    stmt = select(AIMLModel).where(
                        AIMLModel.deployment_state.in_(['live', 'paper', 'shadow'])
                    )
                    result = await db.execute(stmt)
                    active_models = result.scalars().all()
                    
                    if not active_models:
                        logger.debug("No active AI models to monitor for drift")
                    else:
                        drift_detected_count = 0
                        
                        for ai_model in active_models:
                            try:
                                # Run drift detection
                                detector = AIDriftDetector()
                                report = await detector.check_drift(
                                    db=db,
                                    pubsub=pubsub,
                                    model_id=ai_model.id,
                                    lookback_hours=24,
                                )
                                
                                if report and report.drift_detected:
                                    drift_detected_count += 1
                                    logger.warning(
                                        f"Drift detected for {ai_model.model_name}: "
                                        f"score={float(report.drift_score):.4f}, "
                                        f"action={report.action_taken}"
                                    )
                                else:
                                    logger.debug(f"No drift for {ai_model.model_name}")
                                
                            except Exception as e:
                                logger.error(
                                    f"Error checking drift for {ai_model.model_name}: {e}",
                                    exc_info=True
                                )
                        
                        if drift_detected_count > 0:
                            logger.warning(f"Drift detected in {drift_detected_count} models")
                        else:
                            logger.info("No drift detected in any models")
            
            except Exception as e:
                logger.error(f"Drift detection error: {e}", exc_info=True)
            
            # Sleep between iterations
            logger.debug(f"Sleeping for {settings.DRIFT_CHECK_INTERVAL_SECONDS}s")
            await asyncio.sleep(settings.DRIFT_CHECK_INTERVAL_SECONDS)
    
    except asyncio.CancelledError:
        logger.info("Drift detection loop cancelled")
        raise
    finally:
        logger.info("Drift detection loop stopped")
