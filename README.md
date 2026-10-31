# Stock Desk

개별 주식(타 증권사 보유 + 관심 종목) 분석·관리 화면. 매매 봇(kiwoom-trade)과는 별개이고, 같은 Supabase 로그인을 쓴다.

- 웹: https://jch1696.github.io/stock-desk/ (GitHub Pages, `docs/index.html`)
- 지표: 내부자 매수/매도(SEC Form 4), 발행 주식수 변화·자사주 매입(EDGAR XBRL), ROE·PER·PBR·PEG(EDGAR 재무 + Yahoo 현재가)
- 수집: `collector/collect.py` — GitHub Actions 가 평일 KST 07:30 자동 실행(Secrets: SUPABASE_URL/KEY/EMAIL/PASSWORD). 로컬은 `.env` 로.
- 배포: `python publish.py` (Supabase URL·anon key 주입 → docs/ → push)

표: desk_positions(보유), desk_watch(관심·메모), desk_fundamentals/desk_insider/desk_prices(수집, 로그인 사용자 공용).
