# 디스코드 주식봇

`주식` 채널에 패널 메시지를 하나 만들고, 10초마다 그 메시지를 수정하면서 시세를 보여주는 가상 주식 게임 봇입니다.
버튼으로 매수 / 매도 / 내 자산 / 용돈 / 랭킹을 사용합니다.

## 실행 방법

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 봇을 만들고 토큰을 복사
2. 봇을 서버에 초대 (scope: `bot`, 권한: 메시지 보내기, 임베드 링크, 채널 보기)
3. 서버에 `주식` 이름의 텍스트 채널 만들기
4. 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # DISCORD_TOKEN 채우기
python bot.py
```

## GitHub에 올리기

```bash
git init
git add .
git commit -m "디스코드 주식봇 첫 버전"
git branch -M main
git remote add origin https://github.com/<내아이디>/<저장소이름>.git
git push -u origin main
```

`.env`와 `stock.db`는 `.gitignore`에 들어 있어 올라가지 않습니다. 토큰은 절대 커밋하지 마세요.

## 구조

- 종목은 `bot.py`의 `STOCKS`에서 수정 (가상 종목, 랜덤으로 변동)
- 데이터는 `stock.db`(SQLite)에 저장되어 재시작해도 유지
