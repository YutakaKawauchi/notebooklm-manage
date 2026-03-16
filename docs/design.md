# NotebookLM アーティファクト管理ツール 設計書

## 概要

NotebookLM ノートブック内のアーティファクトを fzf でインタラクティブに管理するスタンドアロンツール。
一覧表示、ダウンロード（後処理付き）、削除をサポートする。

## 要件

- **スタンドアロン**: `notebooklm-py` のみに依存。単一スクリプト構成
- **クロスプラットフォーム**: Windows / macOS / Linux / WSL 対応（Python + fzf subprocess）
- **インタラクティブ**: fzf によるノートブック選択、アーティファクトマルチ選択、操作選択
- **安全設計**: デフォルトは「DL+削除」（バックアップ担保）。「削除のみ」は追加警告
- **後処理**: audio/slides 圧縮、infographic/slides ウォーターマーク除去、infographic アスペクト比保持リサイズ

## 実行方法

```bash
# インタラクティブ（全フロー）— PEP 723 で依存自動解決
uv run manage-artifacts.py

# ノートブック直接指定
uv run manage-artifacts.py --notebook-id <id>

# リスト表示のみ
uv run manage-artifacts.py --list-only

# dry-run（API 呼び出しなし）
uv run manage-artifacts.py --dry-run
```

## フロー

```
┌─────────────────────────────┐
│ 1. ノートブック選択          │
│    fzf 単一選択              │
│    API: notebooks.list()     │
├─────────────────────────────┤
│ 2. アーティファクト一覧      │
│    fzf マルチ選択            │
│    API: artifacts.list()     │
│    ソート: タイプ別→日時順   │
├─────────────────────────────┤
│ 3. 操作選択                  │
│    fzf 単一選択              │
│    💾 DL+削除（デフォルト）   │
│    📥 DLのみ                 │
│    🗑️  削除のみ              │
├─────────────────────────────┤
│ 4. 確認 → 実行 → サマリー   │
└─────────────────────────────┘
```

## fzf インターフェース

### ノートブック選択

```
ノートブック選択
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
操作: Enter=選択  Esc=キャンセル

> My Notebook                          2026-01-01
  Research Project                     2026-02-10
  ...
```

- 入力: `notebook_id\ttitle\tcreated_at` (TSV)
- 表示: `--with-nth 2..` で title + created_at のみ
- 出力: `cut -f1` で notebook_id

### アーティファクト選択

```
アーティファクト管理 — My Notebook
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
全 42 件
操作: Tab=選択/解除  Ctrl-A=全選択  Enter=確定  Esc=キャンセル

  🎵  Audio Deep Dive                          2026-01-01 08:30  completed
> 📊  Infographic Summary                      2026-01-01 08:25  completed
  📝  Blog Report                              2026-01-01 08:20  completed
  📑  Slide Deck                               2026-01-01 08:35  completed
```

- 入力: `artifact_id\ticon\ttitle\tcreated_at\tstatus` (TSV)
- ソート: タイプアイコン順 → 日時昇順
- `--multi --bind "ctrl-a:select-all,ctrl-d:deselect-all"`

### 操作選択

```
操作を選択してください (3 件選択中)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

> 💾  ダウンロード + 削除（推奨）
  📥  ダウンロードのみ
  🗑️   削除のみ（バックアップなし）
```

## ダウンロード

### ディレクトリ構造

```
~/Documents/NotebookArtifacts/    ← ARTIFACT_BACKUP_DIR (Windows デフォルト)
~/NotebookArtifacts/              ← ARTIFACT_BACKUP_DIR (Unix デフォルト)
  └── My_Notebook/                ← ノートブックタイトル（サニタイズ済み）
      ├── 20260101_audio_Deep_Dive.mp4
      ├── 20260101_infographic_Summary.png
      ├── 20260101_report_Blog_Report.md
      └── 20260101_slide_deck_Slides.pdf
```

ファイル名: `{created_date}_{type}_{title_sanitized}.{ext}`

### タイプ別ダウンロードメソッド

| タイプ | メソッド | 拡張子 | 後処理 |
|--------|---------|--------|--------|
| audio | `download_audio()` | .mp4 | ffmpeg 圧縮 (64k AAC) |
| infographic | `download_infographic()` | .png | ウォーターマーク自動検出除去 + 1/2 リサイズ |
| report | `download_report()` | .md | なし |
| slide_deck | `download_slide_deck()` | .pdf | ghostscript 圧縮 + ウォーターマーク除去 |
| data_table | `download_data_table()` | .csv | なし |
| quiz | `download_quiz()` | .json | なし |
| flashcards | `download_flashcards()` | .json | なし |
| video | `download_video()` | .mp4 | なし |
| mind_map | `download_mind_map()` | .json | なし |

### 後処理

圧縮ツール未インストール時は警告のみでスキップ（ダウンロード自体は成功）。

- **Audio 圧縮**: ffmpeg → AAC 64kbps
- **Slides 圧縮**: ghostscript → /printer 設定（Windows: `gswin64c` / `gswin32c` を自動検出）
- **Infographic**: PIL で右下隅を走査しウォーターマーク自動検出・除去（原寸） → PIL LANCZOS で 1/2 リサイズ
- **同名ファイル回避**: 並列DL開始前にパスを一括事前計算。ディスク上・バッチ内の重複に連番 `_2`, `_3` を付与
- **Slides ウォーターマーク除去**: pdftoppm → PIL で各ページのウォーターマーク領域を背景色で塗りつぶし → 再PDF化

## 環境変数

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `ARTIFACT_BACKUP_DIR` | `~/Documents/NotebookArtifacts` (Win) / `~/NotebookArtifacts` (Unix) | ダウンロード先ベースディレクトリ |
| `NOTEBOOKLM_TIMEOUT` | `90` | NotebookLM API HTTP タイムアウト (秒) |
| `NO_EMOJI` | - | `1` に設定すると絵文字なし（ASCII のみ） |

環境変数の永続設定:

```powershell
# Windows (PowerShell)
[Environment]::SetEnvironmentVariable("ARTIFACT_BACKUP_DIR", "D:\NotebookArtifacts", "User")
```

```bash
# Linux / macOS
echo 'export ARTIFACT_BACKUP_DIR=~/NotebookArtifacts' >> ~/.bashrc
```

## クロスプラットフォーム対応

| 項目 | Windows | macOS / Linux |
|------|---------|---------------|
| asyncio | `WindowsSelectorEventLoopPolicy` | デフォルト |
| エンコーディング | `PYTHONUTF8=1` 自動設定 | デフォルト UTF-8 |
| subprocess | `encoding="utf-8"` 明示（cp932 回避） | デフォルト UTF-8 |
| ghostscript | `gswin64c` / `gswin32c` 自動検出 | `gs` |
| 絵文字 | Windows Terminal: OK / レガシーcmd: ASCII | OK |
| パス | `pathlib.Path`（バックスラッシュ自動） | `pathlib.Path` |

## 依存関係

### 必須

- Python 3.10+
- `notebooklm-py` >= 0.3.4 (PEP 723 で自動インストール)
- `fzf` (CLI ツール)

### 推奨（後処理用）

- `ghostscript` — slides (PDF) 圧縮。未インストール時は圧縮スキップ（10-20MB → 5-10MB の削減効果あり）
- `pdftoppm` (poppler) — slides ウォーターマーク除去（PDF→画像変換）

### オプション

- `ffmpeg` — audio 圧縮
- `Pillow` — infographic/slides ウォーターマーク除去 + infographic リサイズ（notebooklm-py に同梱）

## Windows セットアップ手順

検証済み手順（Windows 11 + PowerShell + Windows Terminal）:

```powershell
# 1. uv インストール
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. fzf インストール（必須）
winget install fzf

# 3. Playwright ブラウザインストール（認証用）
uvx --from notebooklm-py --with playwright playwright install chromium

# 4. 認証（ブラウザが開く → Google ログイン → Enter）
uvx --from notebooklm-py --with playwright notebooklm login
# ※ リダイレクトエラーが発生する場合は WSL で認証してファイルをコピー:
#    cp ~/.notebooklm/storage_state.json /mnt/c/Users/<username>/.notebooklm/

# 5. ghostscript インストール（推奨 — PDF 圧縮）
# https://ghostscript.com/releases/gsdnld.html からインストーラーをダウンロード
# インストール後: gswin64c --version で確認

# 6. poppler インストール（推奨 — スライド WM 除去）
irm get.scoop.sh | iex
scoop install poppler
# pdftoppm -v で確認

# 7. 出力先ディレクトリを設定（任意）
[Environment]::SetEnvironmentVariable("ARTIFACT_BACKUP_DIR", "D:\NotebookArtifacts", "User")

# ※ PowerShell を再起動して PATH を反映

# 8. 実行
git clone https://github.com/YutakaKawauchi/notebooklm-manage.git
cd notebooklm-manage
uv run manage-artifacts.py
```

## 既知の問題

### Windows: `notebooklm login` のリダイレクトエラー

Google アカウントに既存セッションがある場合、Playwright のナビゲーションが Google のリダイレクトと競合してエラーになることがある。

**回避策**: WSL/Linux 側で `notebooklm login` を実行し、`~/.notebooklm/storage_state.json` を Windows 側にコピーする。

### Windows: fzf の cp932 エンコーディング

日本語 Windows では `subprocess.run(text=True)` がデフォルトで cp932 を使用する。
絵文字や一部の Unicode 文字が `UnicodeEncodeError` になるため、`encoding="utf-8"` を明示的に指定して解決。
