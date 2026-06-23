# chzzk-discord-watcher

CHZZK 채널 상태를 주기적으로 확인하고 Discord webhook으로 방송 알림을 보내는 Python 단일 스크립트 봇입니다.

## 현재 알림

- 방송 시작
- 방송 종료
- 방송 제목 변경
- 방송 카테고리 변경
- 시청자 임계값 돌파

## 대상 채널

| 이름 | CHZZK channel_id |
| --- | --- |
| 믕비 | `a872c0594e60f943748d76c565dd3a07` |
| 연소화 | `bc630aa88c2753dbac14a09fd4901890` |
| 연후 | `1e546b3f42edad3012dc8a64eca1668d` |
| 규비 | `28cbc9a7f252b4bccf6fc479caca686e` |

## 설정

`config.yaml`에서 채널, 임계값, webhook 환경변수 이름을 관리합니다.

운영 환경에서는 webhook URL을 파일에 직접 쓰지 말고 환경변수나 GitHub Secrets로 주입합니다.

필요한 GitHub Secrets:

- `WEBHOOK_MBEUNG`
- `WEBHOOK_YEONSOHWA`
- `WEBHOOK_YEONHU`
- `WEBHOOK_GYUBI`

주요 옵션:

- `viewer_thresholds`: 시청자 수가 해당 값을 처음 넘을 때 알림을 보냅니다.
- `persist_viewer_count`: 기본값 `false`. live 중 시청자 수 변화만으로 `state.json` 커밋이 계속 생기지 않게 합니다.
- `poll_interval_seconds`: 상시 실행 환경에서 사용할 폴링 간격입니다. 현재 GitHub Actions 운영은 workflow 내부 루프가 5분 간격을 담당합니다.

## 로컬 실행

```bash
python -m pip install aiohttp pyyaml python-dotenv
python monitor_chzzk.py config.yaml --state state.json --dry-run
```

`--dry-run` 또는 `DRY_RUN=true`는 Discord webhook 전송과 state 파일 쓰기를 생략합니다.

상세 로그가 필요하면 `--verbose` 또는 `VERBOSE=true`를 사용합니다.

## GitHub Actions 운영

현재 workflow는 `.github/workflows/chzzk-watcher.yml`에서 매시간 한 번 시작되고, job 내부에서 5분 간격으로 최대 12회 watcher를 실행합니다. 각 cycle 후 `state.json`이 변경되면 main 브랜치에 `chore(state)` 커밋을 남깁니다.

수동 실행 시 `cycles` 입력으로 반복 횟수를 줄일 수 있습니다. 예를 들어 테스트는 `cycles=1`로 실행하면 한 번만 확인하고 종료합니다.

주의할 점:

- GitHub Actions의 scheduled workflow는 정확한 5분 타이머가 아닙니다.
- 공개/무료 shared runner 상황, GitHub 부하, repository activity, queue 상태에 따라 실행이 지연될 수 있습니다.
- cron은 "최소 예약 시각"에 가깝고, 실제 시작 시각은 1시간 이상 밀릴 수 있습니다.
- 이 repo는 GitHub schedule이 실제로 1~6시간까지 밀린 이력이 있어, workflow 내부에서 5분 루프를 돌려 지연을 완화합니다.
- 그래도 scheduled workflow 자체가 몇 시간 동안 시작되지 않는 경우까지 완전히 해결하지는 못합니다.

## 최소 개선안

현재 구조를 유지하면서 적용할 수 있는 개선입니다.

- workflow를 명시적인 cycle 루프로 구성합니다. 백그라운드 실행 후 `kill`하는 방식은 사용하지 않습니다.
- `state.json`만 변경됐을 때만 커밋합니다.
- `persist_viewer_count: false`로 시청자 수 변동만으로 발생하는 커밋 노이즈를 줄입니다.
- transient API 실패를 방송 종료로 오인하지 않도록, 모든 API 조회가 실패하면 state 갱신을 건너뜁니다.
- Discord webhook 실패는 로그에 남기고 프로세스 exit code를 실패로 반환합니다.
- `--dry-run`으로 webhook 없이 로컬 점검이 가능합니다.

## 구조 개선안

알림 지연을 줄이려면 GitHub Actions schedule 대신 상시 실행 또는 더 안정적인 스케줄러로 옮기는 편이 현실적입니다.

| 후보 | 장점 | 단점 | 상태 저장 |
| --- | --- | --- | --- |
| VPS + systemd timer/service | 가장 예측 가능하고 단순함 | 서버 관리 필요 | 로컬 JSON, SQLite, Redis |
| Fly.io | 장시간 실행 앱에 적합, 배포 단순 | 무료 한도/슬립 정책 확인 필요 | 볼륨, LiteFS, 외부 DB |
| Render Worker/Cron | 설정이 쉬움 | 무료 플랜 슬립/cron 지연 가능 | Disk, 외부 DB |
| Railway | 배포 경험이 좋음 | 비용 예측 확인 필요 | Volume, Postgres |
| Cloudflare Workers + Cron Triggers | 운영 부담 작고 cron 안정적 | Python 스크립트 직접 이전 불가, JS/TS 재작성 필요 | KV, D1 |

권장 순서:

1. 짧은 기간은 GitHub Actions 개선판으로 유지합니다.
2. 알림 지연이 계속 문제라면 VPS 또는 Fly.io worker로 이전합니다.
3. 상태 저장은 초기에는 JSON/SQLite로 충분하지만, 여러 인스턴스나 장애 복구가 필요해지면 Redis/Postgres/KV로 옮깁니다.

상시 실행 환경 변수:

- `WEBHOOK_MBEUNG`
- `WEBHOOK_YEONSOHWA`
- `WEBHOOK_YEONHU`
- `WEBHOOK_GYUBI`
- `CONFIG_PATH` optional
- `STATE_PATH` optional
- `DRY_RUN` optional
- `VERBOSE` optional

## 배포 이전 메모

상시 실행으로 옮길 때는 스크립트를 loop 방식으로 바꾸거나, 플랫폼 cron이 `python monitor_chzzk.py config.yaml --state state.json`을 주기적으로 호출하게 구성합니다. 중복 실행을 막기 위해 단일 인스턴스 보장 또는 lock 파일/DB lock을 추가하는 것이 좋습니다.
