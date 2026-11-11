"""Application factory; importing the module never calls KIS or opens a DB."""

import asyncio
import hmac
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.clients.kis import KisClient
from app.domain import RiskError, now_kst
from app.repositories.sqlite import Repository
from app.schemas.risk import GradeResponse, PackageRequest, PackageResponse, StockClassificationResponse, Ticker
from app.services.calendar import TradingCalendar
from app.services.risk import RiskService
from app.services.runtime import BatchRuntime, start_scheduler
from app.universe import load_universe

logger = logging.getLogger(__name__)
CLASSIFICATION_TIMEOUT_SECONDS = 45


def create_app(settings: Settings | None = None, clock=now_kst, *, runtime=None, scheduler_factory=start_scheduler,
               classification_client_factory=KisClient):
    settings = settings or Settings.from_env()
    runtime = runtime or BatchRuntime(settings, clock=clock)
    classification_client = None
    classification_request_lock = None
    classification_executor = None
    classification_future = None

    def query_stock_classification(ticker):
        nonlocal classification_client
        started = time.perf_counter()
        try:
            if classification_client is None:
                classification_client = classification_client_factory(
                    settings.kis_key, settings.kis_secret, settings.kis_environment)
            elevated = classification_client.stock_elevated(ticker)
        except RiskError as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            logger.warning("KIS stock classification failed ticker=%s code=%s elapsed_ms=%s",
                           ticker, exc.code, elapsed_ms)
            raise
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            logger.error("KIS stock classification unexpected failure ticker=%s error_type=%s elapsed_ms=%s",
                         ticker, type(exc).__name__, elapsed_ms)
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if elapsed_ms >= 3000:
            logger.warning("KIS stock classification slow ticker=%s elapsed_ms=%s", ticker, elapsed_ms)
        return elevated

    def close_classification_client():
        nonlocal classification_client
        if classification_client is not None:
            classification_client.close()
            classification_client = None

    def consume_late_future_exception(future):
        if not future.cancelled():
            future.exception()

    @asynccontextmanager
    async def lifespan(_):
        nonlocal classification_client, classification_executor, classification_future, classification_request_lock
        scheduler = None
        classification_request_lock = asyncio.Lock()
        classification_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="risk-kis")
        classification_future = None
        try:
            if settings.scheduler_enabled:
                scheduler = scheduler_factory(runtime)
            yield
        finally:
            runtime.stop()
            if scheduler is not None:
                # Let the current stock finish atomically, then leave others pending.
                await asyncio.to_thread(scheduler.shutdown, wait=True)
            executor = classification_executor
            if executor is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(executor, close_classification_client)
                classification_executor = None
                classification_future = None
                classification_request_lock = None
                await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)

    api = FastAPI(title="Risk Service", version="0.3.0", lifespan=lifespan)
    api.state.batch_runtime = runtime

    @api.exception_handler(RiskError)
    async def controlled_error(_, exc: RiskError):
        return JSONResponse(status_code=exc.status, content={"error": {
            "code": exc.code, "message": exc.message, "tickers": exc.tickers,
        }})

    @api.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        # Pydantic errors may contain customer-supplied non-finite values: never echo input.
        return JSONResponse(status_code=400, content={"error": {
            "code": "INVALID_REQUEST", "message": "요청 필드와 비중을 확인하세요.", "tickers": [],
        }})

    def service():
        if settings.calendar is None:
            raise RiskError("CALENDAR_UNAVAILABLE", "RISK_CALENDAR_PATH 설정이 필요합니다.", 503)
        if not settings.database.is_file():
            raise RiskError("STORAGE_UNAVAILABLE", "배치로 초기 데이터를 준비하세요.", 503)
        calendar = TradingCalendar.from_csv(settings.calendar, settings.trading_hours)
        return RiskService(Repository(settings.database), calendar, load_universe(settings.universe), clock(), settings.weight_tolerance)

    def authorize(key: str | None = Header(default=None, alias="X-Risk-Service-Key")):
        if not settings.service_key:
            raise RiskError("AUTH_CONFIGURATION", "서비스 인증 설정이 필요합니다.", 503)
        if key is None or not hmac.compare_digest(key.encode('utf-8'), settings.service_key.encode('utf-8')):
            raise RiskError("UNAUTHORIZED", "서비스 인증이 필요합니다.", 401)

    @api.get('/health')
    def health():
        status = {"status": "DEGRADED", "lastSuccessfulCalculationAt": None, "lastBatchStatus": None,
                  "batch_running": None}
        try:
            status['batch_running'] = runtime.batch_running()
            if settings.database.is_file():
                status.update(Repository(settings.database).health())
            if status['lastBatchStatus'] == 'RUNNING' and status['batch_running'] is False:
                # Persisted checkpoint after a killed process, not a live batch.
                status['lastBatchStatus'] = 'INTERRUPTED'
            calendar = TradingCalendar.from_csv(settings.calendar, settings.trading_hours) if settings.calendar else None
            current = calendar.latest(clock()) if calendar else None
            with Repository(settings.database).snapshot() as db:
                grades = Repository.grades(db)
            usable = any(calendar.fresh(g.price_to, current) for g in grades.values()) if current else False
            if (usable and settings.service_key and not runtime.last_error
                    and status['lastBatchStatus'] not in {'FAILED', 'PARTIAL', 'INTERRUPTED'}):
                status['status'] = 'OK'
        except RiskError as exc:
            logger.warning("Health details unavailable: %s", exc.code)
            pass
        # Liveness remains 200 even when calculations fail; do not cause restart loops.
        return status

    @api.get("/grades", response_model=list[GradeResponse], dependencies=[Depends(authorize)])
    def grades(risk: RiskService = Depends(service)):
        return risk.grades()

    @api.get('/stocks/{ticker}/classification', response_model=StockClassificationResponse,
             dependencies=[Depends(authorize)],
             description='Current stock rule classification using a live KIS status check. '
                         'Independent of price-history freshness; contains no VaR or volatility. '
                         'Provider failure returns an error, never a cached normal classification.')
    async def stock_classification(ticker: Ticker, response: Response):
        nonlocal classification_future
        response.headers['Cache-Control'] = 'no-store'
        instrument = load_universe(settings.universe).get(ticker)
        if instrument is None or instrument.status != 'active' or instrument.kind != 'STOCK':
            raise RiskError('STOCK_CLASSIFICATION_UNAVAILABLE', 'Active domestic stock is required.', 422, [ticker])
        # Reject overlap instead of queuing callers behind a slow KIS request.
        if classification_future is not None and not classification_future.done():
            raise RiskError('KIS_QUERY_BUSY', 'KIS 조회가 진행 중입니다. 잠시 후 다시 시도하세요.', 503, [ticker])
        if classification_request_lock is None:
            raise RiskError('KIS_CONFIGURATION', 'KIS 조회 서비스가 준비되지 않았습니다.', 503, [ticker])
        async with classification_request_lock:
            if classification_future is not None and not classification_future.done():
                raise RiskError('KIS_QUERY_BUSY', 'KIS 조회가 진행 중입니다. 잠시 후 다시 시도하세요.', 503, [ticker])
            if classification_executor is None:
                raise RiskError('KIS_CONFIGURATION', 'KIS 조회 서비스가 준비되지 않았습니다.', 503, [ticker])
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(classification_executor, query_stock_classification, ticker)
            classification_future = future
            try:
                elevated = await asyncio.wait_for(
                    asyncio.shield(future), timeout=CLASSIFICATION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                logger.warning("KIS stock classification timed out ticker=%s timeout_seconds=%s",
                               ticker, CLASSIFICATION_TIMEOUT_SECONDS)
                raise RiskError('KIS_TIMEOUT', 'KIS 조회 시간이 초과되었습니다.', 503, [ticker]) from exc
            finally:
                if future.done():
                    if not future.cancelled():
                        future.exception()
                    if classification_future is future:
                        classification_future = None
                else:
                    future.add_done_callback(consume_late_future_exception)
        if type(elevated) is not bool:
            raise RiskError('GRADING_METADATA_REQUIRED', 'Current stock status is required.', 503, [ticker])
        return StockClassificationResponse(ticker=ticker, riskGrade=1 if elevated else 2, asOf=clock())

    @api.post("/package/volatility", response_model=PackageResponse, dependencies=[Depends(authorize)])
    def volatility(request: PackageRequest, risk: RiskService = Depends(service)):
        return risk.volatility(request)

    return api


app = create_app()
