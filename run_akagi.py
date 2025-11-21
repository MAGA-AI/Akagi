import logging
import sys
from pathlib import Path
from akagi.akagi import main
# ===== ログ設定 =====
# logs フォルダが無ければ作成
Path("logs").mkdir(exist_ok=True)

# ロガー設定（ファイル & ターミナル両方）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/akagi.log", encoding="utf-8"),
        # logging.StreamHandler(sys.stdout)
    ]
)

if __name__ == "__main__":
    logging.info("=== Akagi 起動 ===")
    main()
