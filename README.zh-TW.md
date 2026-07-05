# textbook-to-note

把你自己的 PDF 教科書變成 AI 可搜尋的知識庫，以及結構化、逐條引用的 Obsidian 筆記，連圖片一起處理。

[English README → README.md](README.md)

## 這是什麼

想用 AI 助理讀大部頭教科書，會遇到三個問題：直接把 PDF 餵給大模型又慢又貴；掃描頁或編碼有問題的頁面，模型會安靜地漏掉內容；而圖片（往往是一章最重要的部分）會完全消失。

這套 pipeline 在本機解決這三件事，重活幾乎不花任何 LLM token：

1. **轉檔**（`converter/`）— PDF/EPUB → 乾淨的分章 markdown。文字抽取用 PyMuPDF + pdfplumber（每頁約 130 毫秒、0 token），掃描頁走 OCR 階梯備援（Surya → PaddleOCR-VL → 本地視覺模型 → 最後才輪到大模型視覺）。內建靜默失敗偵測，抓出「看起來抽到字、其實是亂碼」的頁面。
2. **建索引**（選用）— 在本機建語意搜尋（semantic search）索引（LanceDB + bge-m3 embeddings，經由 ollama），讓你的 AI 助理可以跨書搜尋，而不是整本從頭讀到尾。
3. **寫筆記**（`workflows/`）— 結構化的 AI 筆記流程：先立骨架再展開，每一條主張都引用到書名＋章節，AI 自己補充的內容明確標記為推論，最後再與你既有的筆記合併。
4. **抽圖片**（`figures/`）— 按需抽取單張圖，配上決定論的 QC gate：幾何比對、留白／文字滲入／OCR 檢查，並可選用本地視覺模型引導重試。AI 不能「看起來對就好」，過不過關由 gate 決定。

## 設計成讓 AI 幫你部署

你大概是想讓「你的 AI」來裝這套系統，這正是預設的使用方式：

> 把 Claude Code（或任何有能力的 coding agent）指到這個 repo，然後說：
> **「讀 AGENTS.md，幫我把它裝起來。」**

[`AGENTS.md`](AGENTS.md) 是直接寫給 agent 看的步驟：裝依賴、設定環境變數、轉第一本書、安裝兩個 Claude Code skill、跑筆記流程。

想自己動手的話，手動安裝說明在 [`docs/architecture.md`](docs/architecture.md)。

## 目錄結構

```
converter/    PDF/EPUB → markdown 轉換腳本（進入點 convert.py）
figures/      圖片抽取 + QC gate（進入點 figure_remap.py）
skills/       可直接放入 Claude Code 的 skill 定義（textbook-to-md、figure-remap）
workflows/    筆記撰寫流程 prompt（可改造成你自己的筆記系統）
docs/         架構說明、OCR 階梯、書本校準指南
examples/     目標筆記格式的範例
shared/       環境變數驅動的設定（config.py）
```

## 需求

- Python 3.10+，`pip install -r requirements.txt`
- 數位原生 PDF（最常見的情況）只用 CPU 就能跑
- 選用（處理掃描書與圖片 QC）：NVIDIA GPU + [Surya OCR](https://github.com/VikParuchuri/surya)、[ollama](https://ollama.com) 加一個小視覺模型（如 `minicpm-v:8b`）與 `bge-m3` embeddings，全部在本機執行，資料不出你的電腦
- 在 Windows 11 與 macOS 上測試過；Windows 特有的坑都寫在程式碼註解裡

## 設計哲學

- **本機優先、省 token**：昂貴的大模型只留給它真正擅長的事（綜合寫筆記），絕不拿來逐頁讀書。
- **決定論的關卡，不靠 AI 感覺**：每張圖、每個 OCR 頁面都先過規則式 QC，AI 才有資格下判斷。閾值永遠不為了讓失敗案例過關而調整。
- **沒有引用就不算數**：筆記裡每條主張都帶出處（書＋章節）。AI 用自己知識補充的內容一律明確標記。

## 請用你自己的書

這個工具**不含任何教科書內容**。它處理的是你自己擁有的 PDF：買的電子書、機構授權下載、開放授權教材（如 [OpenStax](https://openstax.org)），或在當地法律允許下自己掃描的紙本書。請尊重書籍的授權條款。

## 授權

MIT © 陳柏威 Po-Wei Chen（[drpwchen](https://github.com/drpwchen)）
