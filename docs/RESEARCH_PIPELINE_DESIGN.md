# リサーチ単位・段階分析パイプライン設計

## 1. 目的

PatentViewerは、独立した共有PDFプールにある同じ特許文献を、リサーチごとに異なる自社技術・調査目的・対象集合で評価する。PDFはファイル数と容量が大きいためリサーチフォルダへ複製しない。PDF原本は共有する一方、比較評価、Embedding、クラスタ、マップ配置、レビューはリサーチ境界の内側に保存する。

10B程度までのローカルLLMには、旧版で実績のある「1回につき1判断または1要約」の小さな仕事だけを要求する。複雑なチャンク読解JSON、構成要件分解、要件対応表は生成させない。各応答へ専用の小さなJSON Schemaを適用し、生成後もPythonで同じ契約を再検証する。

## 2. 変えてはいけない表示契約

脅威マップは5×5マトリクスを維持する。

- 横軸 `similarity`：自社技術との類似度、整数1～5
- 縦軸 `concept_level`：請求項の権利範囲の広さ・限定の少なさ、整数1～5
- 主根拠：独立請求項と自社技術の直接比較
- 補助根拠：技術分野、課題、用語定義、構成関係を理解するための本文箇所
- Embedding：技術／課題クラスタ計算の内部補助。脅威マップの座標そのものにはしない

各座標には類似度理由と概念レベル理由を残し、説明可能にする。

## 3. データ境界

### 共有してよいもの

`runtime/shared/extractions/<PDF SHA-256>/` に、PDFから機械的に得られる次の情報を保存する。

- `extracted_text.txt`
- `pages.json`
- `extraction_manifest.json`
- PDFハッシュ、抽出日時、抽出器、ページ数、文字数、OCR有無

キャッシュは効率化のための派生成果物であり、原本ではない。`patent_pool/` のPDFを常に正とする。

### リサーチごとに分離するもの

- `research.json`：自社技術、既定の読取方針、5×5評価方針
- `patent_list_{yyyymmddHHMMSS}.csv`：本番環境の優先入力。最新タイムスタンプ1件をCP932で直接読み込む。入力仕様は [PATENT_LIST_CSV_SPEC.md](PATENT_LIST_CSV_SPEC.md) を参照
- `subresearches/*/patents.json`：CSVがない既存・デバッグ用リサーチの互換入力
- `subresearches/*/pipeline/<patent-id>/`：文献別の段階成果物
- `clustering/<run-id>/`：リサーチ全対象を母集団としたクラスタ計算・命名
- `subresearches/*/results/<patent-id>.json`：UI用の確定結果
- `runs/<run-id>/run_manifest.json`：実行条件、段階、成否、成果物参照

サブリサーチは対象文献の分類・絞り込み単位である。クラスタの既定スコープはサブリサーチ単位ではなく、リサーチ全体とする。

自社技術プロンプトは、当面ユーザーが各リサーチフォルダの `company_tech.txt` へUTF-8テキストとして格納する。分析時は `company_tech.txt` を優先し、存在しない既存リサーチだけ `research.json` の `company_technology` へフォールバックする。将来はアプリ上でアップロードまたは編集し、対象リサーチへの保存、版管理、分析への反映まで画面内で完結させる。

## 4. 原文とキャッシュの選択

リサーチ既定値は `research.json` の `pipeline.source_policy`、文献別上書きは `patents.json` の各文献に `source_policy` を置く。

```json
{
  "pdf": "JPA 2026000001-000000.pdf",
  "source_policy": {
    "extraction": "reextract",
    "reading": "hierarchical_full"
  }
}
```

抽出方針：

- `cache`：PDFハッシュが一致する共有抽出を使用
- `verify_original`：原本ハッシュを検査して共有抽出を使用。不一致・欠落なら再抽出
- `reextract`：原本PDFから強制的に再抽出

読取方針：

- `targeted`：請求項と技術分野・背景・課題・解決手段などの関連章を読む
- `hierarchical_full`：全文を分割して全チャンクを読み、後段で統合する
- `adaptive`：章・請求項の欠落時は全文読解へ昇格し、それ以外は関連章を読む

実際の判断は文献別の `source_decision.json` に確定保存する。PDFハッシュ変更、抽出文字不足、請求項欠落、章欠落、低確信度、人手指定は再抽出または全文読解へ昇格する理由になる。

## 5. 工程と成果物

1. `extract`：Python/pypdfでページ・全文を抽出し、共有キャッシュへ保存する。
2. `structure`：Pythonの規則で章と請求項を分け、独立／従属を仮判定する。`document_structure.json`。
3. `similarity`：独立請求項を中心とする入力と自社技術をLLMが直接比較し、1～5と短い理由だけを返す。`similarity.json`。
4. `concept_level`：独立請求項の限定の少なさをLLMが1～5と短い理由で返す。`concept_level.json`。
5. `summaries`：課題要約と技術要約を別々のLLM要求で作り、Pythonが `summaries.json` に統合する。
6. `embeddings`：技術要約と課題要約を別々にベクトル化する。`embeddings.json`。
7. `clustering`：Pythonで技術ベクトルと課題ベクトルを別々にクラスタ計算する。スコープはリサーチ全体。
8. `cluster_names`：各クラスタの要約群だけをLLMに渡し、name 1項目だけを生成する。
9. `finalize`：検証済み成果物から既存UI互換の7フィールドJSONを原子的に確定する。

LLMへ渡す本文はPythonが用途別に組み立て、各要求を最大12,000文字に制限する。類似度と技術要約は独立請求項、技術分野、解決手段、実施形態を使用し、概念レベルは独立請求項を優先する。課題要約は背景技術、発明の課題、効果を使用する。

PDFが存在しない文献、またはテキスト抽出量が品質基準を下回る画像中心PDFは、リスト行を削除せず分析状態を `skipped` とする。理由はそれぞれ `pdf_not_found`、`image_only_or_insufficient_text` とし、UIの対象文献一覧と件数に残す。現段階ではOCRを自動実行せず、将来OCRを追加する場合も精度・所要時間・使用モデルを監査対象とする。

### 請求項構造の品質ゲート

- 請求項見出しは行頭の `【請求項N】` と括弧付き同等表記だけを認識する。本文中の「請求項1に記載」は見出しとしない。
- 原文に補正前後など複数の請求項集合が含まれる場合は、`1..N` の最長連番ブロックを採用し、除外見出し数を監査情報に残す。
- `document_structure.json` はスキーマ版2とし、`claim_validation.valid`、連番、空本文、従属先番号を検査する。
- 請求項0件、非連番、空本文、不正な従属先がある文献は `invalid_claim_structure` とし、LLMへ渡さない。
- LLM事前診断は、未準備文献がなく、少なくとも1件の有効な構造JSONがある場合に `READY` とする。`skip.json` に理由が記録された文献は全体開始を妨げず、残りの有効文献を処理する。

文献の最大ページ数や最大容量は固定値で切り捨てず、抽出文字数と用途別入力サイズで制御する。LLMのコンテキスト窓は常時最大値に固定せず、プロンプト文字数から `4096 / 8192 / 16384` の最小適合バケットを選ぶ。LLM出力上限は通常分析で384、512、768トークンの順に再試行し、クラスタ名は128、192、256トークンとする。

LLMの各要求・応答は文献別の `attempts/<run-id>/llm_calls/` に監査保存し、再実行時にも過去の失敗監査を削除しない。文献固有の失敗は `analysis_error.json` に最新状態を保存し、その文献を当該runではスキップして次文献へ進む。次回runでは失敗文献を再試行する。PDF欠落、抽出不足、請求項構造異常は `skip.json`、run全体の失敗・スキップ一覧は `run_manifest.json` に保存する。人手確認済み結果は明示的な上書き指定なしに置換しない。

## 6. 情報表現の分離

- 権利評価：独立請求項を中心に、類似度と限定の少なさを別々に評価する
- 技術表現：入力、処理、推定、制御・出力、システム構成を中心に全文の章別読解を統合する
- 課題表現：従来技術の不足と問題発生条件を中心とし、解決手段を混ぜない

明細書本文は請求項を理解する補助であり、実施形態の記載によって請求項の必須構成や権利範囲を勝手に拡張・縮小しない。

## 7. 実行

```powershell
# 計画だけを作り、PDFやLLMには触れない
python tools/run_research_pipeline.py laser_process_landscape --stage plan

# 共有抽出、章・請求項分割、読取判断まで
python tools/run_research_pipeline.py laser_process_landscape --stage prepare

# Ollama段階分析、Embedding、リサーチ全体クラスタ、UI結果確定まで
python tools/run_research_pipeline.py laser_process_landscape --stage execute
```

UIの一括分析ではアプリ専用Ollamaを空きローカルポートで管理する。GPU名や16/24GBラベルではなく、総VRAM、空きVRAM、予約領域、モデルとKVキャッシュの見積りから生成並列数、Embeddingバッチ数、冷却間隔を決定する。生成は同じコンテキストクラスのタスクをまとめ、生成完了後に一度だけEmbeddingモデルへ切り替える。`--cooldown-seconds` の待機は一定文献相当の生成呼出しごとに行い、`keep_alive`を維持して再ロードを発生させない。文献別 `analysis_progress.json` に完了タスクを保存し、停止・障害後は未完了タスクだけを再実行する。文献固有のJSON生成・Schema検証・Embedding検証エラーは `analysis_error.json` に記録し、その文献だけを当該runでスキップする。キャンセル、Ollama接続障害、全体クラスタリング障害は文献スキップへ変換せず、全体を停止する。実行履歴は最新5件とするが、文献別の実行監査はrun IDごとに保持する。500件を超える集合は全ペア距離を作らず、固定射影とMiniBatch cosine K-meansでクラスタリングする。

既存のUI結果を意図して置換する場合だけ `--overwrite` を付ける。通常リサーチ全件の前にDEBUGの少数文献で、類似度と概念レベルの理由が独立請求項と整合すること、5×5の説明可能性、技術／課題要約の混同がないことを人が承認する。

## 8. 可視UI協働

画面上部の `Pipeline` から、リサーチの対象件数、準備・分析・確定件数、既定の抽出／読取方針、preflight状態を確認できる。可視DOMには次の協働targetを公開する。

- `pipeline-open`、`pipeline-refresh`
- `pipeline-prepare`
- `pipeline-execute-confirm`、`pipeline-execute`
- `pipeline-job-pause`、`pipeline-job-resume`、`pipeline-job-cancel`

Codexによる開始は、実行中の可視UIコマンドと同一client・environmentであることをサーバーが検証する。LLM実行はpreflightの全項目成功と可視チェックボックスによる明示確認を必須とする。BridgeのPause、Resume、Cancelはバックエンドジョブのcontrol fileへ伝播し、各文献・各LLM呼出しの境界で協調停止する。長いLLM HTTP呼出しそのものは途中中断せず、応答後の次checkpointで制御を反映する。

## 9. 技術マップの表示閾値

技術マップでは、クラスタ計算・命名・文献へのクラスタ割当を先に全件分保持し、UIの「最小クラスタ件数」で表示だけを切り替える。選択値を `N` とした場合、現在のリサーチおよび検索・年・ステイタス等の絞り込み結果に対して、所属文献が `N` 件未満の技術クラスタと課題クラスタを軸から除外する。クラスタ成果物の再計算や削除は行わず、セルを選択していない文献一覧にも影響させない。

既定値は `1`（すべて表示）、選択肢は `1 / 2 / 3 / 5 / 10` 件以上とする。閾値変更時に技術マップのセル選択が残っている場合は、非表示セルを参照し続けないようセル選択だけを解除する。協働操作では `technology-cluster-min-size` targetへの `select` を公開する。
