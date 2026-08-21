# 3cdog-perf · 給 AI 助理的安裝書

> **你是 AI 助理嗎？** 跟著這份文件一步步做。每節都有「我要做什麼」與「怎麼判斷成功」。
> 做完每一步把輸出貼回對話，方便紀錄問題。
> **不要跳過任何一節**——前面沒做對，後面會卡住。
> 本書只適用 macOS（Apple Silicon 或 Intel 都行）；Windows / Linux 暫不支援。

---

## 0. 摘要（給你看的 TL;DR）

- 套件名：`3cdog-perf`
- 裝法：Homebrew + 公開 tap（`brew tap walkpod1007/3cdog && brew install perf-lab`，或簡寫 `brew install walkpod1007/3cdog/perf-lab`；裝完的指令名仍是 `3cdog-perf`）
- 啟動：`3cdog-perf`，瀏覽器會自動打開 `http://127.0.0.1:8765`
- 失敗模式：八成是「線只能充電不能傳資料」或「Android 沒開 USB 偵錯」

---

## 1. 前置檢查

**我要做什麼**：在終端機跑下面這些指令，確認使用者電腦符合需求。

```bash
sw_vers              # macOS 版本（12+ 即可）
uname -m             # 處理器（arm64 = Apple Silicon，x86_64 = Intel）
which brew           # Homebrew 位置；如果沒輸出表示沒裝
brew --version       # Homebrew 版本
```

**怎麼判斷成功**：

- `sw_vers` 第一行像 `ProductName: macOS` 且後面有版本號。
- `uname -m` 是 `arm64` 或 `x86_64`。
- `which brew` 至少印一個路徑（典型 `/opt/homebrew/bin/brew` 或 `/usr/local/bin/brew`）。
- `brew --version` 印版本（建議 4.x 以上，本書用 6.0.17 實測）。

**若失敗**：
- Homebrew 沒裝 → 引導使用者跑 [brew.sh](https://brew.sh) 上的指令，**不要自己 sudo**。命令大致是：
  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
  Apple Silicon 裝完會在 `/opt/homebrew`；Intel 在 `/usr/local`。確認 `which brew` 有輸出再回來。

---

## 2. 安裝

**我要做什麼**：在終端機跑下面這一條，**不要修改**。

```bash
brew tap walkpod1007/3cdog && brew install perf-lab
```

跑完會輸出像：

```
==> Installing 3cdog-perf from local tap
...
🍺  /opt/homebrew/Cellar/3cdog-perf/0.2.0: N files, X.XMB
==> Running `brew cleanup 3cdog-perf`...
```

**怎麼判斷成功**：

- 最後幾行沒有 `Error:` 或 `Permission denied`。
- 終端機回到 `$ ` prompt。

**若失敗**：
- 出現 `Error: /opt/homebrew is not writable` → 該機器 Homebrew 是別人裝的（owner 不是當前使用者），請使用者登入原本裝 Homebrew 的帳號，或請系統管理員調整權限。**不要 sudo**。
- 出現 `python@3.12 not installed` 且 brew 卡住 → 等他編完（可能 3-5 分鐘）；若環境連不到 PyPI 拉 wheel，請使用者檢查網路。
- 出現 `brew install` 卡在 `python@3.12` 很久但最終失敗 | 可能是 pipx 沒裝或網路問題 | 請使用者確認 `which pipx` 有輸出；裝 pipx：`brew install pipx && pipx ensurepath`，重開 shell 後再裝一次 pymobiledevice3：`pipx install pymobiledevice3`
- 其他錯誤 → 把完整輸出貼回對話，**不要繼續往下做**。

---

## 3. 驗證

**我要做什麼**：跑三個獨立檢查，確認工具真的裝得起來。

### 3.1 版本

```bash
3cdog-perf --version
```

預期輸出：

```
3cdog-perf 0.2.0
```

退出碼 0。

### 3.2 啟動 server（背景跑）

```bash
3cdog-perf --no-open-browser --port 18765 --host 127.0.0.1 &
SERVER_PID=$!
sleep 2
echo "PID=$SERVER_PID"
```

預期：印出 `3cdog-perf UI: http://127.0.0.1:18765` 然後不回 prompt（這是 server 在前景 listen）。

### 3.3 onboarding endpoint

```bash
curl -fsS http://127.0.0.1:18765/api/onboarding/state | python3 -m json.tool
```

預期 JSON 結構：

```json
{
    "android": {
        "adb_installed": true|false,
        "device_state": "device"|"disconnected"|"unauthorized"|"...",
        "device_serial": "..."|null,
        "next_step": "..."
    },
    "ios": {
        "pymobiledevice3_installed": true|false,
        "device_count": 0,
        "devices": [],
        "next_step": "..."
    },
    "ready_to_record": true|false,
    "checked_at": <int>
}
```

`android` 跟 `ios` 兩個鍵必須存在；其他欄位 server 會根據當下狀態填。

### 3.4 首頁關鍵字

```bash
curl -sS http://127.0.0.1:18765/ | grep -c "onboarding\|引導"
```

預期：≥ 1（通常 10+ 命中，因為 overlay 卡文案很多）。

### 3.5 收尾

```bash
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
```

---

## 4. 排障（逐條對照）

| 症狀 | 可能原因 | 修法 |
|------|----------|------|
| `brew install` 卡在 `Installing python@3.12` 很久 | 沒現成 bottle，要現編 | 等，或請使用者確認 `/opt/homebrew` 在 SSD |
| `3cdog-perf --version` 顯示 `command not found` | brew 的 bin 沒進 PATH | 終端機跑 `eval "$(brew shellenv)"`，或重開一個 shell |
| `curl` 印 `Could not connect to 127.0.0.1 port 18765` | server 沒起來 / 還在啟動 | `sleep 2` 再試；或檢查 `--host` 跟 `--port` |
| `/api/onboarding/state` 回 500 | 程式碼 bug（不是環境問題） | 把完整輸出貼回對話 |
| onboarding 卡 Android 永遠 `disconnected` | 線只能充電、或是手機沒開 USB 偵錯 | 換一條線；或到「設定 → 開發人員選項」開 USB 偵錯 |
| onboarding 卡 iOS 永遠 `device_count: 0` | 沒裝 pymobiledevice3、或 iPhone 沒按「信任」 | `brew install pipx && pipx ensurepath`（重開 shell）再 `pipx install pymobiledevice3`；手機接上時按「信任」 |
| 瀏覽器沒自動打開 | SSH / 無桌面環境 | 取消 `--no-open-browser` 預設；或手動連 `http://127.0.0.1:8765` |
| Android 看到 `unauthorized` | 手機沒按「允許 USB 偵錯」 | 手機跳出對話框按「允許」；或拔線重接 |
| iOS 17+ 卡住 | 沒跑 `sudo pymobiledevice3 remote tunneld` | 另開一個終端機視窗跑那條指令並保持開著 |

---

## 5. 裝置引導接手指引（從「進到 Lab」之後）

裝置連線成功、按了「進到 Lab」之後，AI 助理可以這樣接手指引使用者：

### 5.1 選 session

左側欄上方是 session 選擇器。預設是「掃描中…」狀態，按「↻ 重新掃描」可以重抓。**沒有歷史 session 是正常的**——本輪是第一次錄。

### 5.2 自動偵測前景 App

左上有一顆「🎯 自動偵測前景 App」。請使用者：
1. 先在手機上開啟想要測的 App（遊戲 / YouTube / 其他）。
2. 回到瀏覽器按那顆按鈕。

server 會跑 `adb shell dumpsys activity activities`（Android）或讀 bundle id（iOS），
把目前的前景 App package / bundle id 帶入。這比手選省一堆時間。

### 5.3 開始錄製

按「● 錄製」。此時：
- server spawn 一個子行程跑 collector（Android）；iPhone 本輪不會啟動 collector。
- 右上角 REC 時鐘開始走。
- Overview / FPS Lab 兩頁開始更新圖表。

### 5.4 結束

按「■ 停止」：
- 子行程被收掉，session 寫成 `.jsonl` 存在 `~/.3cdog-perf/sessions/`。
- session 出現在左側選擇器裡，之後可以重播（切到 REPLAY 模式）。
- 匯出按鈕（CSV / JSON / FPS Summary）變可用。

### 5.5 驗證資料

匯出 CSV 後可以用 `head` 看一下欄位：

```bash
head -3 ~/.3cdog-perf/sessions/<session-id>.jsonl
```

預期第一行是 schema 標頭，第二行起是 samples。

---

## 6. 升級 / 解除安裝

```bash
brew upgrade 3cdog-perf         # 升級
brew uninstall 3cdog-perf       # 解除安裝（session 資料會留在 ~/.3cdog-perf/）
rm -rf ~/.3cdog-perf            # 連 session 一起清掉
```

---

## 7. 已知限制

- **iPhone 錄製尚未整合**。本輪 iPhone onboarding 只做通道準備（裝 pymobiledevice3、tunneld、信任流程），點「進到 Lab」之後只能預覽，沒辦法錄 iPhone。
- **macOS only**。Apple Silicon 原生支援；Intel macOS 12+ 也行，但 pymobiledevice3 在 Rosetta 下偶爾會撞到 usbmuxd，建議 Apple Silicon。
- **單一 collector**：server 同時只能跑一個錄製子行程；想同時錄兩台裝置請分次跑。

---

## 8. 回報問題

把以下三項貼回對話：
1. `3cdog-perf --version` 輸出
2. `curl -sS http://127.0.0.1:8765/api/onboarding/state` 完整 JSON
3. 哪一步失敗、完整終端機輸出
