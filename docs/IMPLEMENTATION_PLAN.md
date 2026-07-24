# PatentViewer 工程作業計画書

更新日: 2026-07-16

## 1. 到達点

`patent_pool/` を共有・読み取り専用のPDF格納庫とし、`researches/` 配下の各リサーチにある最新の `patent_list_{yyyymmddHHMMSS}.csv` から対象文献を参照する。CSVがないデータではリサーチ直下の `patents.json` を入力とする。ブラウザUIではリサーチ、出願年範囲、権利化・審査中・公開状態を切り替え、脅威マップと技術マップへ即時反映し、セルからPDFをプレビューできるようにする。

ローカルLLMはこの工程では実行しない。抽出・分析・Embedding・クラスタリングを差し込めるジョブ契約、プロンプト、JSON保存先までを準備し、実行前診断で不足条件を確認できる状態を完成条件とする。

## 2. 設計原則

- PDFは `patent_pool/` の一意な文献IDで参照し、リサーチフォルダへ複製しない。
- 通常環境とDEBUG環境は、入力マニフェスト、成果物、監査ログの物理パスを分離する。
- DEBUGは専用fixtureだけを使い、通常成果物へ書き込めないパス検査をサーバー側で行う。
- UI操作は人間とCodexで共通化し、全主要要素に意味ベースの `data-agent-id` を付ける。
- Codex操作は可視ブラウザが実行し、意図・進捗・pause/resume/cancel・結果を共有パネルへ表示する。
- LLM出力はモデル名、プロンプト版、入力PDFハッシュ、実行IDを伴うJSONとして保存し、古いキャッシュを誤用しない。
- 不明値を0点へ丸めず `pending` として扱い、分析済み文献だけをマップ評価へ含める。

## 3. ディレクトリ契約

```text
patent_pool/                         # 共有PDF（アプリから書込禁止）
researches/<research>/
  patent_list_yyyymmddHHMMSS.csv    # 本番の調査対象（CP932）
  company_tech.txt                  # リサーチ固有の自社技術（UTF-8）
  research.json                     # 任意。表示名、説明、パイプライン方針
  patents.json                      # JSON入力時の文献ID、分類、任意の手動メタデータ
  pipeline/<patent-id>/             # 文献別の段階成果物
  results/<patent-id>.json          # UI用の確定結果
debug_data/
  researches/...                    # DEBUG専用入力fixture
  results/...                       # DEBUG専用解析fixture
runtime/normal/                     # 通常の監査・解釈ノート・将来のジョブ状態
runtime/debug/                      # DEBUGの監査・解釈ノート・ジョブ状態
src/patent_viewer/                  # HTTP/API/ドメイン処理
public/                             # HTML/CSS/JS UIとCodex Bridge
tests/                              # 単体・API・分離・UI契約テスト
```

## 4. 工程と完了判定

### Phase 1: 土台とデータ契約

1. Python標準ライブラリでlocalhost専用サーバーを作る。
2. 文献ID正規化、PDF実在確認、重複検査、パストラバーサル拒否を実装する。
3. リサーチ探索、文献入力、分析JSONマージを実装する。
4. 既存PDFから初期リサーチマニフェストを作る。

完了判定: APIからリサーチ一覧と文献一覧を取得でき、不正PDFパスが拒否される。

### Phase 2: PatentViewer UI

1. 添付イメージの濃紺・シアン・角丸カードをデザイントークン化する。
2. リサーチ、出願年の開始・終了、法的状態、検索を即時フィルタにする。
3. 脅威マップ、技術マップ、件数指標、文献一覧、詳細・解釈パネルを連動する。
4. PDFを同一画面のモーダルでプレビューし、別タブ表示も提供する。
5. キーボード操作、フォーカス表示、狭い画面への折返しを実装する。

完了判定: フィルタ操作がリロードなしで両マップと一覧へ反映し、セルからPDFを開ける。

### Phase 3: Codex UI協働

1. heartbeatで現在のclient、environment、capabilities、可視targetを発見可能にする。
2. `click/input/check/select/keydown/wait/assert` の小さな操作語彙を実装する。
3. queued→running→completed/failed/cancelled、イベント履歴、claimを実装する。
4. 共有パネルで意図、対象、ステップ、ログ、pause/resume/cancelを見せる。
5. Codex由来の解釈保存は、実行中の可視コマンドとclient/environment照合を必須にする。

完了判定: Codexコマンドが可視DOMを通って完了し、直接のCodex書込APIは403になる。

追加実装では、リポジトリ同梱stdio MCP、高水準の意味ブロック、dry-run、負荷分類、冪等性、構造化エラー、永続監査、切断検出、ブラウザ再読込復帰、target差分heartbeat、long polling、全Codex更新の可視コマンド認可、ポータブルlauncher/doctorを導入した。既存UIと既存の細粒度Bridge APIは互換経路として維持する。詳細は [CODEX_COLLABORATION.md](CODEX_COLLABORATION.md) を正とする。

### Phase 4: DEBUG分離とテスト

DEBUGテストスイートを次の順に定義する。

1. `unit`: マニフェスト、フィルタ、集計、パス安全性。
2. `api`: research/dashboard/PDF/notes/UI protocolの応答契約。
3. `isolation`: DEBUG書込先が `runtime/debug` だけで、normalのハッシュが不変。
4. `bridge`: heartbeat、claim、状態遷移、pause/resume/cancel、直接書込拒否。
5. `browser-smoke`: 実ブラウザで環境切替、フィルタ、マップセル、PDFモーダル、Bridgeパネル。
6. `llm-preflight`: Ollama到達性、必要モデル、プロンプト、選択PDF、書込先、既存結果衝突を診断（LLMは実行しない）。

実行許可条件: 1〜5が成功し、6が `ready` または「Ollama/モデル未準備」だけを明示した `blocked` であること。DEBUGからNORMALへ戻したことも終了条件とする。

### Phase 5: ローカルLLM接続（今回の停止点）

今回作るもの: ジョブ入力・結果JSONスキーマ、プロンプト版、preflight、実行ボタンの無効状態と説明。

次回、人間の明示操作後に行うもの: Ollama呼出し、PDF抽出、4分析、Embedding、クラスタリング、結果の原子的保存。112件の一括処理前にDEBUG少数件で品質を承認する。

### Phase 5 実証記録（2026-07-16）

人間の明示依頼により、DEBUG専用リサーチの実PDF 1件で接続を実証した。`gemma4:e4b` による7フィールドの分析、`qwen3-embedding:8b` による2本の4096次元Embedding、監査JSON、UI反映まで成功した。NORMAL成果物は変更していない。次の判断点は、複数文献へ拡大する前の請求項照合と要約品質レビューである。

### Phase 6: リサーチ単位・段階パイプライン

単発実証用の一括プロンプトは後方互換のため残し、複数文献の通常処理には `tools/run_research_pipeline.py` を使用する。昨晩の長時間バッチで複雑なチャンク読解JSONと構成要件JSONが出力上限に達したため、この方式は廃止した。通常処理は、共有抽出、規則ベースの章・請求項分割、類似度、概念レベル、課題要約、技術要約、2種Embedding、リサーチ全体クラスタ、短いクラスタ名、UI結果確定に分離する。LLMの各出力は1～2項目の小さなJSON Schemaで生成制約し、Pythonでも再検証する。

コードと契約の実装後、DEBUG実PDF 1件で新方式の `execute` を検証した。4つの生成応答はすべて初回でSchema適合し、`done_reason=stop`、出力246トークン以下で完了した。2要約のEmbedding、クラスタ命名、UI結果確定まで成功し、文献失敗・スキップは0件だった。複数文献の `execute` は長時間実行と人手品質承認を伴うため、まだ通常成果物へ実行していない。詳細設計は [RESEARCH_PIPELINE_DESIGN.md](RESEARCH_PIPELINE_DESIGN.md) を正とする。

## 5. 受入基準

- サーバーは `127.0.0.1` 以外へ既定でbindしない。
- `patent_pool/` をアプリが変更しない。
- NORMAL/DEBUGの入力と出力が画面・API・ファイルパスで一致する。
- 年次・状態・検索条件の変更が両マップ、指標、一覧へ即時反映する。
- 文献IDから元PDFを安全に参照し、UI内プレビューできる。
- pending分析を分析済み件数やスコアに混入させない。
- Codexは現在の可視targetを発見して操作し、人間が停止できる。
- テスト方法、データ形式、LLM接続手順がREADMEから辿れる。

## 6. 将来拡張候補

- 書誌情報CSV/J-PlatPatエクスポートの取込と法的状態履歴。
- 引用・被引用ネットワーク、ファミリー単位の重複排除。
- クラスタ版間差分、スコア根拠比較、人手レビューと確信度。
- 保存済みフィルタ、調査スナップショット、Markdown/CSVエクスポート。
- OCR品質、請求項1抽出、PDFハッシュ変化の再解析キュー。
