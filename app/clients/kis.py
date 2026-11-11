"""KIS adjusted daily history; secrets and provider payloads never enter errors."""

import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from app.domain import Price, RiskError

BASE_URLS = {
    "demo": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}


class KisClient:
    def __init__(self, key, secret, environment="demo", *, transport=None, sleep=time.sleep):
        if not key or not secret or environment not in BASE_URLS:
            raise RiskError("KIS_CONFIGURATION", "KIS 실행 환경과 인증키를 확인하세요.", 503)
        self.key, self.secret = key, secret
        self.http = httpx.Client(base_url=BASE_URLS[environment], timeout=30, follow_redirects=False, transport=transport)
        self.sleep, self.token = sleep, None

    def close(self):
        self.http.close()

    def _request(self, method, path, **kwargs):
        for attempt in range(3):
            if attempt:
                self.sleep(1.3 * attempt)
            try:
                response = self.http.request(method, path, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    continue
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError
                code = body.get("msg_cd")
                if code == "EGW00201":
                    # 초당 호출 초과. 토큰은 하루 1회 발급 원칙이라 버리지 않고 쉬었다 다시 보낸다.
                    continue
                if code == "EGW00123" and "headers" in kwargs:
                    # 토큰 만료: 새로 받아서 이번 재시도부터 쓴다.
                    self.token = None
                    kwargs["headers"] = {**kwargs["headers"], "authorization": self._headers()["authorization"]}
                    continue
                return body
            except httpx.TransportError:
                continue
            except (httpx.HTTPError, ValueError) as exc:
                raise RiskError("KIS_RESPONSE_ERROR", "시세 제공기관 응답을 처리할 수 없습니다.", 503) from exc
        raise RiskError("KIS_UNAVAILABLE", "시세 조회 재시도 횟수를 초과했습니다.", 503)

    def _headers(self):
        if self.token is None:
            body = self._request("POST", "/oauth2/tokenP", json={
                "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret,
            })
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise RiskError("KIS_AUTH_FAILED", "KIS 인증에 실패했습니다.", 503)
            self.token = token
        return {"authorization": "Bearer " + self.token, "appkey": self.key,
                "appsecret": self.secret, "tr_id": "FHKST03010100", "custtype": "P"}

    def history(self, ticker, start, end):
        prices, cursor = {}, end
        for _ in range(20):
            data = self._request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                 headers=self._headers(), params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            })
            if str(data.get("rt_cd")) != "0":
                raise RiskError("KIS_QUERY_FAILED", "종목 시세 조회에 실패했습니다.", tickers=[ticker])
            try:
                reported = data.get("output1", {}).get("stck_shrn_iscd", "")
                if reported and reported != ticker:
                    raise ValueError
                batch = data["output2"]
                if not isinstance(batch, list):
                    raise ValueError
                batch = [row for row in batch if row.get("stck_bsop_date")]
                if not batch:
                    break
                page = []
                for row in batch:
                    day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                    if day in prices or not start <= day <= cursor:
                        raise ValueError
                    price = Price(day, Decimal(row["stck_clpr"]), int(row["acml_vol"]))
                    prices[day] = price
                    page.append(day)
                if page != sorted(page) and page != sorted(page, reverse=True):
                    raise ValueError
                earliest = min(page)
                if earliest <= start:
                    break
                if earliest >= cursor:
                    raise ValueError
                cursor = earliest - timedelta(days=1)
            except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation) as exc:
                raise RiskError("INVALID_KIS_DATA", "시세 날짜·코드·가격·거래량 검증에 실패했습니다.", tickers=[ticker]) from exc
        else:
            raise RiskError("KIS_PAGE_LIMIT", "시세 페이지 한도를 초과했습니다.", tickers=[ticker])
        return sorted(prices.values(), key=lambda p: p.day)

    def stock_elevated(self, ticker):
        """Current KRX-listed stock warning/management status; never assume normal."""
        headers = {**self._headers(), 'tr_id': 'FHKST01010100'}
        data = self._request('GET', '/uapi/domestic-stock/v1/quotations/inquire-price',
            headers=headers, params={'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        try:
            if str(data.get('rt_cd')) != '0':
                raise ValueError
            row = data['output']
            if row['stck_shrn_iscd'] != ticker:
                raise ValueError
            warning, managed = row['mrkt_warn_cls_code'], row['mang_issu_cls_code']
            if warning not in {'00', '01', '02', '03'} or managed not in {'Y', 'N'}:
                raise ValueError
            return warning != '00' or managed == 'Y'
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskError('GRADING_METADATA_REQUIRED', 'KIS stock classification is missing or invalid.', 503, tickers=[ticker]) from exc

    def etf_metadata(self, ticker):
        """KIS ETF/ETN quote: tracking multiplier and listing-date evidence.

        A listing date is an upper bound on inception, not its replacement.
        Official example: examples_llm/etfetn/inquire_price/inquire_price.py.
        """
        data = self._request('GET', '/uapi/etfetn/v1/quotations/inquire-price',
            headers={**self._headers(), 'tr_id': 'FHPST02400000'},
            params={'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker})
        try:
            if str(data.get('rt_cd')) != '0':
                raise ValueError
            row = data['output']
            multiple = Decimal(row['etf_trc_ert_mltp'])
            listed_on = datetime.strptime(row['stck_lstn_date'], '%Y%m%d').date()
            if not multiple.is_finite() or multiple == 0:
                raise ValueError
            # Reference 3 defines leveraged/inverse ETFs as targeting >1x or a negative multiple;
            # keep -1x inverse in scope instead of using abs(multiple) > 1.
            return {'listed_on': listed_on, 'leveraged_or_inverse': multiple < 0 or multiple > 1,
                    'tracking_multiple': str(multiple)}
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise RiskError('GRADING_METADATA_REQUIRED', 'KIS ETF metadata is missing or invalid.', 503, tickers=[ticker]) from exc
