"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
"""

import argparse
import sys
from dotenv import load_dotenv
load_dotenv()

from datetime import date

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        # 优先用东方财富 API（秒级完成），失败回退 baostock 多进程
        count = engine.sync_today_eastmoney()
        if count < 0:
            logger.warning("东方财富 API 失败，回退到 baostock 多进程同步...")
            count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings)

        # 5. 遍历策略，有结果则推送至对应机器人
        all_selected: dict[str, list[str]] = {}  # {symbol: [strategy_name, ...]}

        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if selected:
                notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                )
                for sym in selected:
                    all_selected.setdefault(sym, []).append(strategy_name)
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

        # 6. 综合资金面推荐：汇总全策略选股 + 资金流向 → 筛选 2~3 只可买入
        if all_selected:
            logger.info(
                f"综合推荐：候选池 {len(all_selected)} 只（去重后），"
                f"开始查询资金流向..."
            )
            unique_symbols = list(all_selected.keys())
            fund_data = engine.get_fund_flow(unique_symbols)
            logger.info(f"资金流向查询完成，返回 {len(fund_data)} 只")

            scored: list[dict] = []
            for symbol, strat_names in all_selected.items():
                ff = fund_data.get(symbol)
                if not ff:
                    continue

                score = 0
                reasons: list[str] = []
                strat_count = len(strat_names)

                # ① 策略交叉（权重最高）
                score += strat_count * 30
                if strat_count >= 2:
                    reasons.append(f"{strat_count}策略交叉共振")

                # ② 今日主力净流入
                if ff["main_net"] > 0:
                    score += 15
                    if ff["main_pct"] > 3:
                        reasons.append(f"今日主力净流入{ff['main_pct']:.1f}%")

                # ③ 5日主力净流入（中期趋势）
                if ff["net_5d"] > 0:
                    score += 20
                    reasons.append("5日资金持续流入")

                # ④ 10日主力净流入（长期趋势）
                if ff["net_10d"] > 0:
                    score += 10

                # ⑤ 涨跌幅评估（避免追高 / 避免暴跌）
                chg = ff["chg_pct"]
                if -3 <= chg <= 5:
                    score += 15
                    if chg < 0:
                        reasons.append("缩量回调蓄力")
                    else:
                        reasons.append("涨幅温和")
                elif chg > 10:
                    score -= 20  # 追高风险
                elif chg < -8:
                    score -= 15  # 暴跌风险

                # ⑥ 换手率（活跃但不过度投机）
                if 1 <= ff["turnover_rate"] <= 15:
                    score += 10

                scored.append(
                    {
                        "symbol": symbol,
                        "name": ff["name"],
                        "close": ff["close"],
                        "chg_pct": ff["chg_pct"],
                        "main_net": ff["main_net"],
                        "main_pct": ff["main_pct"],
                        "net_5d": ff["net_5d"],
                        "net_10d": ff["net_10d"],
                        "turnover_rate": ff["turnover_rate"],
                        "strategy_count": strat_count,
                        "score": score,
                        "reason": "；".join(reasons) if reasons else "综合评分入选",
                    }
                )

            # 按评分降序，取 top 3（至少有正向资金支持的）
            scored.sort(key=lambda x: x["score"], reverse=True)
            top_picks = [p for p in scored[:3] if p["score"] > 0]

            if top_picks:
                logger.info(
                    f"综合推荐选出 {len(top_picks)} 只："
                    + " / ".join(f"{p['name']}({p['symbol']},评分{p['score']})" for p in top_picks)
                )
                notifier.send_top_picks(top_picks)
            else:
                logger.info("综合推荐：无符合条件的标的，跳过推送")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
