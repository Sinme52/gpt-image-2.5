# gpt-image-2-5

Claude から OpenAI の GPT Image 2.5 (`gpt-image-2.5-sunburst` / `gpt-image-2.5-flare`) を呼び出して画像を作る Claude スキルです。

- 通常は品質 `medium` で下書きを作成
- 「リリース用に」と頼むと、medium の画像を入力にして構図を保ったまま `xhigh` で再出力
- Python 標準ライブラリのみで動作（追加インストール不要）

## 構成

```
gpt-image-2-5/
├── SKILL.md            # Claude 向けの手順
├── scripts/gpt_image.py
└── .env.example
```

## インストール

### claude.ai（Claude Code のクラウドセッションにも自動で読み込まれます）

1. `gpt-image-2-5` フォルダを zip にする

   ```bash
   zip -r gpt-image-2-5.zip gpt-image-2-5 -x '*/.env' '*/__pycache__/*' '*.DS_Store'
   ```

2. claude.ai の 設定 > 機能 > スキル から zip をアップロード

### Claude Code（ローカル）

```bash
cp -r gpt-image-2-5 ~/.claude/skills/
```

## API キーの設定

キーは次の順に探します。

1. 環境変数 `OPENAI_IMAGE_API_KEY`
2. スキルフォルダ直下の `.env`
3. スクリプトを実行したディレクトリの `.env`

`.env` の書式は `.env.example` を参照してください。`.env` は `.gitignore` で除外しています。

### Claude Code のクラウド環境で使う場合

クラウド環境の設定で次の 2 つを行います。

- **Environment variables**: `OPENAI_IMAGE_API_KEY=sk-...`
- **Network access**: Custom にして Allowed domains に `api.openai.com` を追加（既定の Trusted リストには含まれていません）

## 使い方

Claude に頼むだけで使えます。

- 「雨の夜の喫茶店のイラストを作って」→ medium で下書き
- 「これをリリース用にして」→ xhigh で仕上げ

スクリプトを直接実行することもできます。

```bash
# 下書き (medium)
python3 gpt-image-2-5/scripts/gpt_image.py generate --prompt "..." --size 1536x1024

# リリース (xhigh)。プロンプトとサイズは下書きのサイドカー JSON から引き継ぐ
python3 gpt-image-2-5/scripts/gpt_image.py release --image gpt-image-outputs/draft-xxxx.png
```

主なオプション: `--model gpt-image-2.5-flare`（高速版）、`--background transparent`、`--n 2`、`release --mode regenerate`（元画像を使わずプロンプトから再生成）、`release --quality max`。

## 注意

- API の利用料金は OpenAI アカウントに課金されます。`xhigh` は `medium` より大幅に時間とコストがかかります。
- このスキル専用のキーを作り、権限を画像生成に絞って予算上限を設定しておくことをおすすめします。
