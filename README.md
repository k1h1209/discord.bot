# 아테노스 서버 상태 봇

마인크래프트(Aternos) 서버가 열려있는지 닫혀있는지 디스코드 채널의 메시지 하나로 보여주는 봇입니다.

- 🟢 열려있음 (접속자 수 표시) / 🟡 켜지는 중 / 🔴 닫혀있음
- 1분마다 서버를 확인하고 같은 메시지를 계속 수정합니다.
- `bot.py`의 `EDIT_ONLY_ON_CHANGE`
  - `True`: 상태가 바뀔 때만 수정
  - `False`: 1분마다 항상 수정

## 실행 방법

1. 디스코드 개발자 포털에서 봇을 만들고 서버에 초대합니다.
2. `.env.example`을 `.env`로 복사한 뒤 값을 채웁니다.
   - `DISCORD_TOKEN`: 봇 토큰
   - `CHANNEL_ID`: 상태를 표시할 채널 ID
   - `SERVER_HOST`, `SERVER_PORT`: 마인크래프트 서버 주소와 포트
3. 설치 후 실행합니다.

```bash
pip install -r requirements.txt
python bot.py
```

## 주의

- `.env`(토큰)는 절대 GitHub에 올리지 마세요. `.gitignore`에 이미 들어 있습니다.
- 봇이 계속 동작하려면 봇을 실행하는 컴퓨터(또는 호스팅)가 켜져 있어야 합니다.
