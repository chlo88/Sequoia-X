"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。"""
        import baostock as bs
        bs.login()
        mapping = {}
        for code in symbols:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            rs = bs.query_stock_basic(code=f"{prefix}.{code}")
            while rs.next():
                row = rs.get_row_data()
                mapping[code] = row[1]  # 第2个字段是股票名称
        bs.logout()
        return mapping

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 缅A | Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**日期：** {today}\n**策略：** {strategy_name}\n**选股数量：** {len(symbols)}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_card(symbols, strategy_name)

        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # 解析飞书真正的返回体
            resp_json = resp.json()

            # 飞书真正的成功标志是内部的 code == 0
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")

        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")

    # ── 综合资金面推荐 ──

    @staticmethod
    def _fmt_amount(wan: float) -> str:
        """将万元金额格式化为易读字符串：>=1亿显示'X.XX亿'，否则'XX万'。"""
        if abs(wan) >= 10000:
            return f"{wan / 10000:.2f}亿"
        return f"{wan:.0f}万"

    def _build_top_picks_card(self, picks: list[dict]) -> dict:
        """构建综合推荐飞书卡片。

        Args:
            picks: 综合筛选后的推荐列表，每项含 name/symbol/close/chg_pct/
                   main_net/net_5d/net_10d/strategy_count/score/reason。
        """
        today = date.today().strftime("%Y-%m-%d")

        elements: list[dict] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**日期：** {today}\n**候选池：** 综合全策略选股 + 资金流向筛选",
                },
            },
            {"tag": "hr"},
        ]

        medals = ["🥇", "🥈", "🥉"]
        for i, p in enumerate(picks):
            medal = medals[i] if i < len(medals) else "🔹"
            xq_code = self._to_xueqiu_code(p["symbol"])
            chg_icon = "🔺" if p["chg_pct"] >= 0 else "🔻"
            main_icon = "🔴" if p["main_net"] >= 0 else "🟢"
            net5d_icon = "🔴" if p["net_5d"] >= 0 else "🟢"

            content = (
                f"{medal} **[{p['name']}](https://xueqiu.com/S/{xq_code})** "
                f"({p['symbol']})\n"
                f"　现价 **{p['close']:.2f}** {chg_icon}{p['chg_pct']:+.2f}%　"
                f"换手 {p.get('turnover_rate', 0):.1f}%\n"
                f"　主力净流入 {main_icon}{self._fmt_amount(p['main_net'])}　"
                f"5日 {net5d_icon}{self._fmt_amount(p['net_5d'])}　"
                f"10日 {self._fmt_amount(p['net_10d'])}\n"
                f"　策略交叉 **{p['strategy_count']}** 个　评分 **{p['score']}**\n"
                f"　💡 {p['reason']}"
            )
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            )
            if i < len(picks) - 1:
                elements.append({"tag": "hr"})

        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "> ⚠️ 以上基于多策略交叉 + 资金流向的客观分析，不构成投资建议。入市有风险，陈哥自决。",
                },
            }
        )

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "🎯 缅A | Sequoia-X 综合推荐 | 资金面精选",
                    },
                    "template": "red",
                },
                "elements": elements,
            },
        }

    def send_top_picks(
        self,
        picks: list[dict],
        webhook_key: str = "default",
    ) -> None:
        """推送综合资金面推荐卡片至飞书群。

        在所有策略选股推送完成后，汇总候选池 + 资金流向 + 综合评分，
        选出 2-3 只可买入标的，作为最后一条推送。

        Args:
            picks: 综合筛选后的推荐列表。
            webhook_key: 飞书 webhook 标识，默认 'default'。
        """
        if not picks:
            logger.info("无综合推荐结果，跳过推送")
            return

        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_top_picks_card(picks)

        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp_json = resp.json()
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"综合推荐推送失败 [{webhook_key}] "
                    f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(
                    f"综合推荐推送成功 [{webhook_key}]，共 {len(picks)} 只股票"
                )
        except requests.RequestException as exc:
            logger.error(f"综合推荐推送请求异常 [{webhook_key}]：{exc}")
