# 3cdog-perf

> 把 Android 跟 iPhone 接到 Mac，按一個鈕就開始抓 FPS、CPU、GPU、PSS、耗電、溫度。
> 給手機評測圈寫的本地 profiler；不傳資料、不開帳號、不收費。

3cdog-perf 是一個跑在你 Mac 上的小工具：把手機用線接上、按下「錄製」，
就會把每一�的流暢度、CPU 使用率、GPU 負載、記憶體（PSS）、耗電跟溫度全部記下來。
結束後直接看 Overview / FPS Lab 兩頁儀表板，要匯出 CSV / JSON 也一鍵搞定。

iPhone 的錄製本輪**還沒接上**——這版只能顯示 iPhone 是否接線、信任狀態、
開發者模式狀態，方便你先熟悉通道；之後會在另一張票接上錄製。

---

## 安裝（兩條路，選一條就好）

### 路徑 A — 一行貼上（終端機跑一次）

打開「終端機」App（Spotlight 搜「Terminal」），貼這行：

```bash
brew tap walkpod1007/3cdog && brew install 3cdog-perf && 3cdog-perf
```

跑完會自動打開瀏覽器。如果瀏覽器沒自動跳，把 `http://127.0.0.1:8765`
貼到瀏覽器網址列就行。

> 第一次用會看到一張「引導卡」，照著做就好——一張 Android、一張 iPhone，
> 哪台接了就把哪台做完，按「進到 Lab」就開始。

### 路徑 B — 貼給你的 AI 助理

如果你平常請 ChatGPT / Claude / Gemini 幫忙裝東西，把下面這行貼過去：

```
請照這份安裝書幫我裝：https://github.com/walkpod1007/3cdog-perf/blob/main/INSTALL-AI.md
```

AI 會照步驟問你 macOS 版本、確認 Homebrew 裝了沒、跑安裝指令、驗證裝得起來，
最後打開 3cdog-perf。**不用自己懂終端機**。

---

## 上手 30 秒

1. **Android**：
   1. 手機「設定 → 關於手機」對「版本號碼」連點 7 次。
   2. 回「設定 → 系統 → 開發人員選項」，把「USB 偵錯」打開。
   3. 用支援資料的線（不要純充電線）接上 Mac。
   4. 手機跳「允許 USB 偵錯嗎」按「允許」。
2. **iPhone**（本輪僅做引導）：
   1. 終端機跑 `brew install pipx && pipx ensurepath`（重開 shell），再 `pipx install pymobiledevice3`。
   2. 接上 Mac，按「信任這部電腦」並輸入螢幕密碼。
   3. iPhone 開「開發者模式」（設定 → 隱私與安全性 → 開發者模式）。
   4. iOS 17 以上還要在終端機跑 `sudo pymobiledevice3 remote tunneld`，
      那個視窗保持開著。
3. **回到瀏覽器**：右側的「裝置狀態」變綠就按「進到 Lab」。
4. **錄製**：按左上「● 錄製」，過幾秒畫圖表就會動起來；按「■ 停止」存檔。

---

## 這東西適合誰

- 寫手機評測、開箱、實測 YouTube / 部落格的朋友。
- 遊戲工作室做競品對標的效能分析。
- App 開發者想看自己產品在別家手機上的真實表現。
- 任何想回答「這台手機跑這個 App 到底多順」的人。

---

## 它不做什麼（避免誤會）

- **不上傳任何資料**。所有 session 都存你 Mac 本地。
- **不開帳號、不登入**。
- **不繞過 iOS 的開發者模式限制**。iPhone 錄製要你主動開開發者模式 + 跑 tunneld，這是 Apple 的規定。
- **Windows 不支援**。這版只做 macOS（M1 / M2 / M3 / M4 都行）。

---

## 常用按鈕

| 按鈕 | 用途 |
|------|------|
| ● 錄製 | 開始抓這一輪 session（FPS / CPU / GPU / PSS / 耗電 / 溫度） |
| ■ 停止 | 收尾、把資料寫成 `.jsonl` |
| ↻ 重新掃描 | 重抓裝置清單 |
| 🎯 自動偵測前景 App | 不用手選 package，自動抓目前開的 App |
| 匯出 CSV / JSON | 給 Excel / 腳本用 |
| FPS Summary | 給評測文章用的 7 個 FPS 指標 |

---

## 排障（卡住看這）

- **看不到 Android 狀態變綠** → 線可能只能充電不能傳資料，換一條；或是手機沒開「USB 偵錯」。
- **iPhone 卡在「請接上 iPhone」** → macOS 沒給「隱私與安全性 → 配件」存取權限，去系統設定允許。
- **`pymobiledevice3` 沒裝** → 終端機跑一次 `brew install pipx && pipx ensurepath`（重開 shell），再 `pipx install pymobiledevice3`。
- **`brew install` 跑不動** → 先確認有裝 Homebrew（[brew.sh](https://brew.sh)）。Apple Silicon 的話安裝目錄會落在 `/opt/homebrew`；Intel 在 `/usr/local`。
- **瀏覽器沒自動打開** → 手動連 `http://127.0.0.1:8765`。

---

## 給工程師的附註

- 套件本體是 Python 3.10+ 的 stdlib-only HTTP server + 單頁 UI，零第三方依賴。
- Homebrew formula 透過 `python@3.12` 拉一個隔離的 Python 環境，裝的只是 3cdog-perf 本體（stdlib-only），所以 brew 沙盒不需要連 PyPI。iPhone 端的 `pymobiledevice3` 由使用者另外用 `pipx` 安裝在自己的環境（見安裝路徑 A 的 caveat 與 onboarding 卡），不污染 system pip。
- Session 檔預設存在 `~/.3cdog-perf/sessions/`，升級不會清掉舊資料。
- Source 同步在 `repo/`，是個標準 PEP 621 `pyproject.toml`。

---

## 授權

MIT。手機圈朋友歡迎拿去改、改完拿去用，不過請保留出處。
