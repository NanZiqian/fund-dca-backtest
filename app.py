"""基金定投策略回测工具

GUI 应用：用户可自定义买/卖规则，输入基金代码获取净值数据，
然后回测在 1月/3月/6月/1年/3年 区间内按策略定投的总收益。

依赖：akshare, pandas, matplotlib, tkinter (内置)
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from dataclasses import asdict, dataclass
from datetime import timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Optional

import pandas as pd

try:
    import matplotlib

    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    HAS_MPL = True
except Exception:
    HAS_MPL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FUNDS_FILE = os.path.join(BASE_DIR, "funds.json")
STRATEGIES_FILE = os.path.join(BASE_DIR, "strategies.json")
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------- Utilities ----------------

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fund_csv_path(code: str) -> str:
    return os.path.join(DATA_DIR, f"{code}.csv")


# ---------------- Data ----------------

def fetch_fund_nav(code: str) -> pd.DataFrame:
    """通过 akshare 获取基金完整历史单位净值。"""
    import akshare as ak

    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    df = df.rename(columns={"净值日期": "date", "单位净值": "nav"})
    df["date"] = pd.to_datetime(df["date"])
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna(subset=["nav"]).sort_values("date").reset_index(drop=True)
    # 用净值自己计算日涨跌幅 (%)，避免源数据格式差异
    df["daily_ret"] = df["nav"].pct_change() * 100.0
    return df[["date", "nav", "daily_ret"]]


def save_fund_csv(code: str, df: pd.DataFrame) -> None:
    df.to_csv(fund_csv_path(code), index=False, encoding="utf-8-sig")


def load_fund_csv(code: str) -> Optional[pd.DataFrame]:
    p = fund_csv_path(code)
    if not os.path.exists(p):
        return None
    return pd.read_csv(p, parse_dates=["date"])


def lookup_fund_name(code: str) -> str:
    """通过 akshare 查询基金简称（带本地缓存）。失败时回退为代码本身。"""
    try:
        import akshare as ak

        cache = os.path.join(DATA_DIR, "_all_funds.csv")
        if not os.path.exists(cache):
            all_df = ak.fund_name_em()
            all_df.to_csv(cache, index=False, encoding="utf-8-sig")
        all_df = pd.read_csv(cache, dtype=str)
        code_col = "基金代码" if "基金代码" in all_df.columns else all_df.columns[0]
        name_col = "基金简称" if "基金简称" in all_df.columns else all_df.columns[1]
        all_df[code_col] = all_df[code_col].astype(str).str.zfill(6)
        row = all_df[all_df[code_col] == code]
        if len(row):
            return str(row.iloc[0][name_col])
    except Exception:
        pass
    return code


# ---------------- Strategy ----------------

@dataclass
class Rule:
    name: str = "新规则"
    frequency: str = "daily"      # daily | weekly | monthly
    weekday: int = 0              # 0=周一..6=周日（weekly 时使用）
    monthday: int = 1             # 1..28（monthly 时使用，非交易日顺延到下一个交易日）
    direction: str = "drop"       # drop=跌 | rise=涨 | any=不限涨跌（无条件触发）
    threshold_pct: float = 2.0    # 触发阈值（正数，单位 %）；direction=any 时忽略
    action: str = "buy"           # buy | sell
    amount: float = 50.0          # 金额（元）


def _freq_ok(rule: Rule, date: pd.Timestamp, month_fired: dict) -> bool:
    if rule.frequency == "daily":
        return True
    if rule.frequency == "weekly":
        return date.weekday() == rule.weekday
    if rule.frequency == "monthly":
        # 当月第一个 day>=monthday 的交易日触发
        key = (rule.name, date.year, date.month)
        if month_fired.get(key):
            return False
        if date.day >= rule.monthday:
            month_fired[key] = True
            return True
        return False
    return False


def rule_matches(rule: Rule, date: pd.Timestamp, daily_ret_pct: float, month_fired: dict) -> bool:
    if not _freq_ok(rule, date, month_fired):
        return False
    if rule.direction == "any":
        return True
    if rule.direction == "drop":
        return (-daily_ret_pct) >= rule.threshold_pct
    return daily_ret_pct >= rule.threshold_pct


def backtest(df: pd.DataFrame, rules: List[Rule], start_date: pd.Timestamp):
    df = df[df["date"] >= start_date].reset_index(drop=True)
    if df.empty:
        return None

    cash_invested = 0.0
    cash_redeemed = 0.0
    shares = 0.0
    transactions = []
    equity_dates: list = []
    equity_values: list = []
    invested_curve: list = []
    nav_curve: list = []
    return_pct_curve: list = []   # 截至当日的收益率(%) = (总价值 - 累计买入) / 累计买入
    month_fired: dict = {}

    for i in range(len(df)):
        row = df.iloc[i]
        date: pd.Timestamp = row["date"]
        nav = float(row["nav"])
        daily_ret = 0.0 if pd.isna(row["daily_ret"]) else float(row["daily_ret"])

        # 先求所有命中的规则，再分组：any (无条件) 全部触发；drop/rise (条件) 同向只取最严格的一条。
        matched_buys = [r for r in rules if r.action == "buy" and rule_matches(r, date, daily_ret, month_fired)]
        matched_sells = [r for r in rules if r.action == "sell" and rule_matches(r, date, daily_ret, month_fired)]

        any_buys = [r for r in matched_buys if r.direction == "any"]
        cond_buys = [r for r in matched_buys if r.direction != "any"]
        any_sells = [r for r in matched_sells if r.direction == "any"]
        cond_sells = [r for r in matched_sells if r.direction != "any"]

        buys_to_fire = list(any_buys)
        if cond_buys:
            buys_to_fire.append(max(cond_buys, key=lambda r: r.threshold_pct))
        sells_to_fire = list(any_sells)
        if cond_sells:
            sells_to_fire.append(max(cond_sells, key=lambda r: r.threshold_pct))

        for buy in buys_to_fire:
            sh = buy.amount / nav
            shares += sh
            cash_invested += buy.amount
            transactions.append({
                "date": date.strftime("%Y-%m-%d"), "action": "买入",
                "rule": buy.name, "amount": buy.amount, "nav": nav,
                "shares": sh, "daily_ret": daily_ret,
            })
        for sell in sells_to_fire:
            amt = min(sell.amount, shares * nav)
            if amt > 1e-6:
                sh = amt / nav
                shares -= sh
                cash_redeemed += amt
                transactions.append({
                    "date": date.strftime("%Y-%m-%d"), "action": "卖出",
                    "rule": sell.name, "amount": amt, "nav": nav,
                    "shares": sh, "daily_ret": daily_ret,
                })

        equity_dates.append(date)
        total_val_today = shares * nav + cash_redeemed
        equity_values.append(total_val_today)
        invested_curve.append(cash_invested - cash_redeemed)
        nav_curve.append(nav)
        if cash_invested > 0:
            return_pct_curve.append((total_val_today - cash_invested) / cash_invested * 100.0)
        else:
            return_pct_curve.append(0.0)

    final_nav = float(df.iloc[-1]["nav"])
    holdings_value = shares * final_nav
    total_value = holdings_value + cash_redeemed
    profit = total_value - cash_invested
    return_pct = (profit / cash_invested * 100.0) if cash_invested > 0 else 0.0
    # 与"等额一次性买入并持有"对比的基准收益率（仅参考）
    first_nav = float(df.iloc[0]["nav"])
    bh_return_pct = (final_nav / first_nav - 1) * 100.0

    # 推荐卖出日：截至当日收益率 > 5% 的日子里，取最高的 3 天（且互相至少间隔 14 天，避免挨在一起）
    SELL_THRESHOLD = 5.0
    MIN_GAP_DAYS = 14
    cand = [
        (i, equity_dates[i], return_pct_curve[i])
        for i in range(len(equity_dates))
        if return_pct_curve[i] > SELL_THRESHOLD
    ]
    cand.sort(key=lambda x: -x[2])
    recommended_sells: list = []
    for i, d, p in cand:
        if any(abs((d - sd).days) < MIN_GAP_DAYS for _, sd, _ in recommended_sells):
            continue
        recommended_sells.append((i, d, p))
        if len(recommended_sells) >= 3:
            break
    recommended_sells.sort(key=lambda x: x[1])  # 按日期升序便于阅读

    return {
        "cash_invested": cash_invested,
        "cash_redeemed": cash_redeemed,
        "net_invested": cash_invested - cash_redeemed,
        "shares": shares,
        "final_nav": final_nav,
        "holdings_value": holdings_value,
        "total_value": total_value,
        "profit": profit,
        "return_pct": return_pct,
        "buy_hold_return_pct": bh_return_pct,
        "transactions": transactions,
        "equity_dates": equity_dates,
        "equity_values": equity_values,
        "invested_curve": invested_curve,
        "nav_curve": nav_curve,
        "return_pct_curve": return_pct_curve,
        "recommended_sells": recommended_sells,
        "trade_count": len(transactions),
        "start_date": df.iloc[0]["date"],
        "end_date": df.iloc[-1]["date"],
    }


# ---------------- GUI ----------------

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("基金定投策略回测")
        self.geometry("1180x820")

        self.funds: dict = load_json(FUNDS_FILE, {})  # {code: name}
        self.strategies: dict = load_json(STRATEGIES_FILE, {})
        self._migrate_strategies()

        self._build_ui()
        self._refresh_preset_combo()
        self._refresh_rules_table()
        self._refresh_fund_combo()

    def _migrate_strategies(self):
        """把旧版 {rules:[...]} 迁移到 {presets:{name:[rules]}, current: name}。"""
        default_rules = [
            asdict(Rule(name="跌2%买50", threshold_pct=2.0, amount=50)),
            asdict(Rule(name="跌3%买100", threshold_pct=3.0, amount=100)),
            asdict(Rule(name="跌4%买200", threshold_pct=4.0, amount=200)),
        ]
        daily_dca_rules = [
            asdict(Rule(name="每日定投30", frequency="daily", direction="any",
                        threshold_pct=0.0, action="buy", amount=30)),
        ]
        s = self.strategies or {}
        if "presets" not in s:
            if "rules" in s and s["rules"]:
                s = {"presets": {"默认": s["rules"]}, "current": "默认"}
            else:
                s = {"presets": {"默认": default_rules}, "current": "默认"}
        if not s["presets"]:
            s["presets"]["默认"] = default_rules
        # 若没有"每日定投"预设，添加一个示例（用户随时可改/删）
        if "每日定投" not in s["presets"]:
            s["presets"]["每日定投"] = daily_dca_rules
        if s.get("current") not in s["presets"]:
            s["current"] = next(iter(s["presets"]))
        self.strategies = s
        save_json(STRATEGIES_FILE, self.strategies)

    @property
    def current_rules(self) -> list:
        return self.strategies["presets"][self.strategies["current"]]

    @current_rules.setter
    def current_rules(self, value: list):
        self.strategies["presets"][self.strategies["current"]] = value

    # --- UI ---
    def _build_ui(self):
        top = ttk.LabelFrame(self, text="基金选择")
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="已保存基金:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.fund_combo = ttk.Combobox(top, width=46, state="readonly")
        self.fund_combo.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        self.fund_combo.bind("<<ComboboxSelected>>", self._on_fund_selected)
        ttk.Button(top, text="删除", command=self._delete_fund).grid(row=0, column=2, padx=4)
        self.data_range_label = ttk.Label(top, text="", foreground="#0a5")
        self.data_range_label.grid(row=0, column=3, padx=10, sticky="w")

        ttk.Label(top, text="基金代码:").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self.code_entry = ttk.Entry(top, width=20)
        self.code_entry.grid(row=1, column=1, padx=4, pady=4, sticky="w")
        self.fetch_btn = ttk.Button(top, text="获取最新数据", command=self._fetch_data)
        self.fetch_btn.grid(row=1, column=2, padx=4)
        self.fetch_status = ttk.Label(top, text="")
        self.fetch_status.grid(row=1, column=3, padx=8, sticky="w")

        mid = ttk.LabelFrame(self, text="策略规则（同向多条命中时只触发阈值最大的那条）")
        mid.pack(fill="x", padx=8, pady=6)

        preset_bar = ttk.Frame(mid)
        preset_bar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(preset_bar, text="策略预设:").pack(side="left")
        self.preset_combo = ttk.Combobox(preset_bar, width=30, state="readonly")
        self.preset_combo.pack(side="left", padx=4)
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Button(preset_bar, text="删除预设", command=self._delete_preset).pack(side="left", padx=4)
        ttk.Button(preset_bar, text="重命名", command=self._rename_preset).pack(side="left", padx=4)

        cols = ("name", "frequency", "weekday", "monthday", "direction", "threshold", "action", "amount")
        headers = ("名称", "频率", "星期", "月日", "方向", "阈值%", "操作", "金额(元)")
        self.rules_tree = ttk.Treeview(mid, columns=cols, show="headings", height=6)
        for c, h in zip(cols, headers):
            self.rules_tree.heading(c, text=h)
            self.rules_tree.column(c, width=100, anchor="center")
        self.rules_tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(mid, command=self.rules_tree.yview)
        sb.pack(side="left", fill="y")
        self.rules_tree.configure(yscrollcommand=sb.set)
        self.rules_tree.bind("<Double-1>", lambda e: self._edit_rule())

        btns = ttk.Frame(mid)
        btns.pack(side="left", fill="y", padx=6)
        for txt, cmd in [("新增", self._add_rule), ("编辑", self._edit_rule),
                          ("删除", self._delete_rule), ("保存策略", self._save_strategies)]:
            ttk.Button(btns, text=txt, command=cmd).pack(fill="x", pady=2)

        bot = ttk.LabelFrame(self, text="回测")
        bot.pack(fill="x", padx=8, pady=6)
        ttk.Label(bot, text="回测起始:").grid(row=0, column=0, padx=4)
        self.period_var = tk.StringVar(value="1y")
        for i, (val, lbl) in enumerate([("1m", "1月"), ("3m", "3月"), ("6m", "6月"),
                                         ("1y", "1年"), ("3y", "3年")]):
            ttk.Radiobutton(bot, text=lbl, variable=self.period_var, value=val).grid(row=0, column=1 + i, padx=2)
        ttk.Button(bot, text="运行回测", command=self._run_backtest).grid(row=0, column=10, padx=10)

        res = ttk.LabelFrame(self, text="结果")
        res.pack(fill="both", expand=True, padx=8, pady=6)

        left = ttk.Frame(res)
        left.pack(side="left", fill="y", padx=4, pady=4)
        self.summary_text = tk.Text(left, width=48, height=24, font=("Consolas", 10))
        self.summary_text.pack(fill="both", expand=True)
        left_btns = ttk.Frame(left)
        left_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(left_btns, text="导出当前结果", command=self._export_current_result).pack(side="left", padx=2)
        ttk.Button(left_btns, text="导出所有回测结果", command=self._export_all_results).pack(side="left", padx=2)
        ttk.Button(left_btns, text="导出所有基金结果", command=self._export_all_funds_results).pack(side="left", padx=2)

        right = ttk.Frame(res)
        right.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        if HAS_MPL:
            self.fig = Figure(figsize=(6, 4.6), dpi=90)
            self.ax = self.fig.add_subplot(111)
            self.ax2 = self.ax.twinx()  # 右轴用于基金单位净值
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            try:
                matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
                matplotlib.rcParams["axes.unicode_minus"] = False
            except Exception:
                pass
            right_btns = ttk.Frame(right)
            right_btns.pack(fill="x", pady=(4, 0))
            ttk.Button(right_btns, text="导出图片", command=self._export_current_image).pack(side="left", padx=2)
            ttk.Button(right_btns, text="导出所有回测图片", command=self._export_all_images).pack(side="left", padx=2)
            ttk.Button(right_btns, text="导出所有基金图片", command=self._export_all_funds_images).pack(side="left", padx=2)
        else:
            ttk.Label(right, text="未安装 matplotlib，无图表显示").pack()

    # --- Fund handling ---
    def _refresh_fund_combo(self):
        items = [f"{name} ({code})" for code, name in self.funds.items()]
        self.fund_combo["values"] = items

    def _on_fund_selected(self, _evt=None):
        val = self.fund_combo.get()
        if "(" in val and val.endswith(")"):
            code = val.rsplit("(", 1)[1].rstrip(")")
            self.code_entry.delete(0, "end")
            self.code_entry.insert(0, code)
            self._update_data_range_label(code)

    def _update_data_range_label(self, code: str):
        df = load_fund_csv(code)
        if df is None or df.empty:
            self.data_range_label.config(text="（本地无数据，请点击 获取最新数据）", foreground="#a00")
            return
        d0 = df["date"].min().strftime("%Y-%m-%d")
        d1 = df["date"].max().strftime("%Y-%m-%d")
        self.data_range_label.config(
            text=f"已获得 {d0} ~ {d1} 的数据（共 {len(df)} 个交易日）",
            foreground="#0a5",
        )

    def _delete_fund(self):
        val = self.fund_combo.get()
        if not val:
            return
        code = val.rsplit("(", 1)[1].rstrip(")")
        if messagebox.askyesno("确认", f"删除 {val}? 同时删除本地缓存。"):
            self.funds.pop(code, None)
            save_json(FUNDS_FILE, self.funds)
            try:
                os.remove(fund_csv_path(code))
            except OSError:
                pass
            self.fund_combo.set("")
            self._refresh_fund_combo()

    def _fetch_data(self):
        raw = self.code_entry.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请输入基金代码（6 位数字）")
            return
        code = raw.zfill(6) if raw.isdigit() else raw
        self.fetch_btn.config(state="disabled")
        self.fetch_status.config(text="获取中…")

        def worker():
            try:
                df = fetch_fund_nav(code)
                save_fund_csv(code, df)
                name = lookup_fund_name(code)
                self.funds[code] = name
                save_json(FUNDS_FILE, self.funds)
                self.after(0, lambda: self._on_fetch_done(code, len(df), None))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self._on_fetch_done(code, 0, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_fetch_done(self, code: str, n: int, err: Optional[str]):
        self.fetch_btn.config(state="normal")
        if err:
            self.fetch_status.config(text="失败")
            messagebox.showerror("失败", f"获取失败：{err}")
            return
        self.fetch_status.config(text=f"已获取 {n} 条")
        self._refresh_fund_combo()
        for v in self.fund_combo["values"]:
            if v.endswith(f"({code})"):
                self.fund_combo.set(v)
                break
        self._update_data_range_label(code)

    # --- Presets ---
    def _refresh_preset_combo(self):
        names = list(self.strategies["presets"].keys())
        self.preset_combo["values"] = names
        self.preset_combo.set(self.strategies["current"])

    def _on_preset_selected(self, _evt=None):
        name = self.preset_combo.get()
        if name and name in self.strategies["presets"]:
            self.strategies["current"] = name
            save_json(STRATEGIES_FILE, self.strategies)
            self._refresh_rules_table()

    def _delete_preset(self):
        name = self.strategies["current"]
        if len(self.strategies["presets"]) <= 1:
            messagebox.showwarning("提示", "至少要保留一个预设")
            return
        if not messagebox.askyesno("确认", f"删除预设 “{name}”？"):
            return
        del self.strategies["presets"][name]
        self.strategies["current"] = next(iter(self.strategies["presets"]))
        save_json(STRATEGIES_FILE, self.strategies)
        self._refresh_preset_combo()
        self._refresh_rules_table()

    def _rename_preset(self):
        old = self.strategies["current"]
        new = simpledialog.askstring("重命名预设", "新名称：", initialvalue=old, parent=self)
        if not new or new == old:
            return
        if new in self.strategies["presets"]:
            messagebox.showwarning("提示", f"已存在名为 “{new}” 的预设")
            return
        # 保持字典顺序：重建
        new_presets = {}
        for k, v in self.strategies["presets"].items():
            new_presets[new if k == old else k] = v
        self.strategies["presets"] = new_presets
        self.strategies["current"] = new
        save_json(STRATEGIES_FILE, self.strategies)
        self._refresh_preset_combo()

    # --- Rules ---
    def _refresh_rules_table(self):
        for i in self.rules_tree.get_children():
            self.rules_tree.delete(i)
        for idx, r in enumerate(self.current_rules):
            self.rules_tree.insert("", "end", iid=str(idx), values=(
                r["name"],
                {"daily": "每日", "weekly": "每周", "monthly": "每月"}.get(r["frequency"], r["frequency"]),
                WEEKDAY_LABELS[r["weekday"]] if r["frequency"] == "weekly" else "-",
                r["monthday"] if r["frequency"] == "monthly" else "-",
                {"drop": "跌", "rise": "涨", "any": "不限"}.get(r["direction"], r["direction"]),
                "-" if r["direction"] == "any" else f'{r["threshold_pct"]}%',
                "买入" if r["action"] == "buy" else "卖出",
                f'{r["amount"]:g}',
            ))

    def _add_rule(self):
        RuleDialog(self, None, lambda d: self._on_rule_saved(d, None))

    def _edit_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        RuleDialog(self, self.current_rules[idx], lambda d: self._on_rule_saved(d, idx))

    def _on_rule_saved(self, rule_dict: dict, idx: Optional[int]):
        rules = self.current_rules
        if idx is None:
            rules.append(rule_dict)
        else:
            rules[idx] = rule_dict
        save_json(STRATEGIES_FILE, self.strategies)
        self._refresh_rules_table()

    def _delete_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        del self.current_rules[idx]
        save_json(STRATEGIES_FILE, self.strategies)
        self._refresh_rules_table()

    def _save_strategies(self):
        """提示输入名称：旧名→提醒覆盖；新名→另存为新预设。"""
        cur = self.strategies["current"]
        name = simpledialog.askstring(
            "保存策略", "预设名称（与现有同名将覆盖；新名称将另存为新预设）：",
            initialvalue=cur, parent=self,
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        rules_snapshot = [dict(r) for r in self.current_rules]
        if name in self.strategies["presets"] and name != cur:
            if not messagebox.askyesno("覆盖确认", f"已存在预设 “{name}”，覆盖它？"):
                return
        if name == cur:
            if not messagebox.askyesno("覆盖确认", f"覆盖当前预设 “{cur}”？"):
                return
            self.strategies["presets"][name] = rules_snapshot
        else:
            self.strategies["presets"][name] = rules_snapshot
            self.strategies["current"] = name
        save_json(STRATEGIES_FILE, self.strategies)
        self._refresh_preset_combo()
        self._refresh_rules_table()
        messagebox.showinfo("已保存", f"策略已保存为 “{name}”")

    # --- Backtest ---
    PERIOD_DAYS = {"1m": 30, "3m": 91, "6m": 183, "1y": 365, "3y": 365 * 3}
    PERIOD_LABELS = {"1m": "1月", "3m": "3月", "6m": "6月", "1y": "1年", "3y": "3年"}

    def _run_one_backtest(self, code: str, period: str):
        """跑一次回测，返回 result 或 None。"""
        df = load_fund_csv(code)
        if df is None or df.empty:
            return None
        start_date = df["date"].max() - timedelta(days=self.PERIOD_DAYS[period])
        rules = [Rule(**r) for r in self.current_rules]
        if not rules:
            return None
        return backtest(df, rules, start_date)

    def _run_backtest(self):
        raw = self.code_entry.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择或获取基金")
            return
        code = raw.zfill(6) if raw.isdigit() else raw
        df = load_fund_csv(code)
        if df is None or df.empty:
            messagebox.showwarning("提示", "本地没有该基金数据，请先点击 “获取最新数据”")
            return
        if not self.current_rules:
            messagebox.showwarning("提示", "尚未配置策略规则")
            return
        period = self.period_var.get()
        result = self._run_one_backtest(code, period)
        if not result:
            messagebox.showwarning("提示", "所选区间内无数据")
            return
        self.last_result = result
        self.last_code = code
        self.last_period = period
        self._render_result(result, code, period)

    def _build_summary_text(self, r: dict, code: str, period: str) -> str:
        name = self.funds.get(code, code)
        lines = [
            f"基金：{name} ({code})",
            f"策略预设：{self.strategies['current']}",
            f"基金：{name} ({code})",
            f"区间：{r['start_date'].strftime('%Y-%m-%d')} ~ {r['end_date'].strftime('%Y-%m-%d')}  [{period}]",
            f"成交次数：{r['trade_count']}",
            "",
            f"累计买入：{r['cash_invested']:>10.2f} 元",
            f"累计卖出：{r['cash_redeemed']:>10.2f} 元",
            f"净投入：  {r['net_invested']:>10.2f} 元",
            f"持有份额：{r['shares']:>10.4f}",
            f"末日净值：{r['final_nav']:>10.4f}",
            f"持仓市值：{r['holdings_value']:>10.2f} 元",
            f"总价值：  {r['total_value']:>10.2f} 元 （持仓 + 已赎回现金）",
            "",
            f"盈亏：    {r['profit']:>+10.2f} 元",
            f"收益率：  {r['return_pct']:>+10.2f}%  （= 盈亏 / 累计买入）",
            f"基准B&H：{r['buy_hold_return_pct']:>+10.2f}%  （区间内单位净值涨跌）",
            "",
            "推荐卖出日（截至当日累计收益率 > 5% 的最高 3 天，间隔 ≥ 14 天）：",
        ]
        if r["recommended_sells"]:
            for _i, d, p in r["recommended_sells"]:
                lines.append(f"  ★ {d.strftime('%Y-%m-%d')}   累计收益率 {p:+.2f}%")
        else:
            lines.append("  （区间内未出现收益率 > 5% 的日子）")
        lines.append("")
        lines.append(f"全部交易（共 {len(r['transactions'])} 条）：")
        for tx in r["transactions"]:
            lines.append(
                f"  {tx['date']} {tx['action']} {tx['amount']:>6.0f}元 "
                f"@净值{tx['nav']:.4f}  日变{tx['daily_ret']:+.2f}%  [{tx['rule']}]"
            )
        return "\n".join(lines)

    def _draw_chart_on(self, fig, ax, ax2, r: dict, code: str, period: str):
        name = self.funds.get(code, code)
        ax.clear()
        ax2.clear()
        l1, = ax.plot(r["equity_dates"], r["equity_values"],
                       label="策略总价值", color="#1f77b4", linewidth=1.6)
        l2, = ax.plot(r["equity_dates"], r["invested_curve"],
                       label="净投入累计", color="#ff7f0e", linestyle="--", linewidth=1.2)
        ax.set_ylabel("元 (策略)")
        ax.grid(True, alpha=0.3)

        l3, = ax2.plot(r["equity_dates"], r["nav_curve"],
                        label="基金单位净值", color="#888", linewidth=1.0, alpha=0.85)
        ax2.set_ylabel("单位净值 (基金)")

        handles = [l1, l2, l3]
        for _i, d, p in r["recommended_sells"]:
            val = r["equity_values"][_i]
            ax.scatter([d], [val], color="red", s=60, zorder=5,
                        edgecolor="darkred", linewidth=1.2)
            ax.annotate(
                f"{d.strftime('%m-%d')}\n{p:+.1f}%",
                xy=(d, val), xytext=(0, 14), textcoords="offset points",
                ha="center", color="red", fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="red", lw=0.8),
            )
        if r["recommended_sells"]:
            from matplotlib.lines import Line2D
            handles.append(Line2D([0], [0], marker="o", color="w",
                                   markerfacecolor="red", markeredgecolor="darkred",
                                   markersize=8, label="推荐卖出日"))

        ax.set_title(f"{name} ({code})  {self.PERIOD_LABELS.get(period, period)} 回测")
        ax.legend(handles=handles, loc="upper left", fontsize=9)
        fig.autofmt_xdate()
        fig.tight_layout()

    def _render_result(self, r: dict, code: str, period: str):
        text = self._build_summary_text(r, code, period)
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", text)
        if HAS_MPL:
            self._draw_chart_on(self.fig, self.ax, self.ax2, r, code, period)
            self.canvas.draw()

    # --- Exports ---
    def _ensure_last_result(self) -> bool:
        if getattr(self, "last_result", None) is None:
            messagebox.showwarning("提示", "请先点击 “运行回测”")
            return False
        return True

    def _export_current_result(self):
        if not self._ensure_last_result():
            return
        code = self.last_code
        period = self.last_period
        default = f"{code}_{period}_结果.txt"
        path = filedialog.asksaveasfilename(
            title="导出当前结果", defaultextension=".txt",
            initialfile=default,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        text = self._build_summary_text(self.last_result, code, period)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        messagebox.showinfo("已导出", f"已写入：\n{path}")

    def _export_all_results(self):
        raw = self.code_entry.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择基金")
            return
        code = raw.zfill(6) if raw.isdigit() else raw
        if not self.current_rules:
            messagebox.showwarning("提示", "尚未配置策略规则")
            return
        df = load_fund_csv(code)
        if df is None or df.empty:
            messagebox.showwarning("提示", "本地无该基金数据")
            return
        path = filedialog.asksaveasfilename(
            title="导出所有回测结果（同一只基金 × 1月/3月/6月/1年/3年）",
            defaultextension=".txt",
            initialfile=f"{code}_所有回测结果.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        parts = []
        for period in ("1m", "3m", "6m", "1y", "3y"):
            result = self._run_one_backtest(code, period)
            parts.append("=" * 60)
            parts.append(f"== {self.PERIOD_LABELS[period]} 回测 ==")
            parts.append("=" * 60)
            if result is None:
                parts.append("（无可用数据）\n")
            else:
                parts.append(self._build_summary_text(result, code, period))
                parts.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        messagebox.showinfo("已导出", f"已写入：\n{path}")

    def _export_current_image(self):
        if not HAS_MPL:
            messagebox.showwarning("提示", "未安装 matplotlib")
            return
        if not self._ensure_last_result():
            return
        code = self.last_code
        period = self.last_period
        path = filedialog.asksaveasfilename(
            title="导出图片", defaultextension=".png",
            initialfile=f"{code}_{period}.png",
            filetypes=[("PNG 图片", "*.png"), ("PDF 文件", "*.pdf"),
                        ("SVG 图片", "*.svg"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.fig.savefig(path, dpi=150, bbox_inches="tight")
        messagebox.showinfo("已导出", f"已写入：\n{path}")

    def _export_all_images(self):
        if not HAS_MPL:
            messagebox.showwarning("提示", "未安装 matplotlib")
            return
        raw = self.code_entry.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择基金")
            return
        code = raw.zfill(6) if raw.isdigit() else raw
        if not self.current_rules:
            messagebox.showwarning("提示", "尚未配置策略规则")
            return
        out_dir = filedialog.askdirectory(title="选择导出目录（每个区间一张图）")
        if not out_dir:
            return
        saved = []
        for period in ("1m", "3m", "6m", "1y", "3y"):
            result = self._run_one_backtest(code, period)
            if result is None:
                continue
            fig = Figure(figsize=(8, 5), dpi=110)
            ax = fig.add_subplot(111)
            ax2 = ax.twinx()
            self._draw_chart_on(fig, ax, ax2, result, code, period)
            out_path = os.path.join(out_dir, f"{code}_{period}.png")
            fig.savefig(out_path, dpi=150, bbox_inches="tight")
            saved.append(out_path)
        if saved:
            messagebox.showinfo("已导出", "已写入：\n" + "\n".join(saved))
        else:
            messagebox.showwarning("提示", "没有可导出的结果")

    def _export_all_funds_results(self):
        """对当前选中的时间区间，把策略跑遍所有已保存的基金，结果合并到一个 .txt。"""
        if not self.funds:
            messagebox.showwarning("提示", "没有已保存的基金")
            return
        if not self.current_rules:
            messagebox.showwarning("提示", "尚未配置策略规则")
            return
        period = self.period_var.get()
        path = filedialog.asksaveasfilename(
            title=f"导出所有基金结果（区间 {self.PERIOD_LABELS[period]}）",
            defaultextension=".txt",
            initialfile=f"所有基金_{period}_结果.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        parts = [
            f"# 所有基金 × {self.PERIOD_LABELS[period]} 回测",
            f"# 策略预设：{self.strategies['current']}",
            "",
        ]
        for code, name in self.funds.items():
            parts.append("=" * 60)
            parts.append(f"== {name} ({code}) ==")
            parts.append("=" * 60)
            df = load_fund_csv(code)
            if df is None or df.empty:
                parts.append("（本地无数据，已跳过）\n")
                continue
            result = self._run_one_backtest(code, period)
            if result is None:
                parts.append("（区间内无数据）\n")
            else:
                parts.append(self._build_summary_text(result, code, period))
                parts.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        messagebox.showinfo("已导出", f"已写入：\n{path}")

    def _export_all_funds_images(self):
        """对当前选中的时间区间，把策略跑遍所有已保存的基金，每只基金导出一张图。"""
        if not HAS_MPL:
            messagebox.showwarning("提示", "未安装 matplotlib")
            return
        if not self.funds:
            messagebox.showwarning("提示", "没有已保存的基金")
            return
        if not self.current_rules:
            messagebox.showwarning("提示", "尚未配置策略规则")
            return
        period = self.period_var.get()
        out_dir = filedialog.askdirectory(
            title=f"选择导出目录（每只基金一张图，区间 {self.PERIOD_LABELS[period]}）"
        )
        if not out_dir:
            return
        saved = []
        skipped = []
        for code, name in self.funds.items():
            df = load_fund_csv(code)
            if df is None or df.empty:
                skipped.append(f"{name} ({code}) - 无本地数据")
                continue
            result = self._run_one_backtest(code, period)
            if result is None:
                skipped.append(f"{name} ({code}) - 区间内无数据")
                continue
            fig = Figure(figsize=(8, 5), dpi=110)
            ax = fig.add_subplot(111)
            ax2 = ax.twinx()
            self._draw_chart_on(fig, ax, ax2, result, code, period)
            out_path = os.path.join(out_dir, f"{code}_{period}.png")
            fig.savefig(out_path, dpi=150, bbox_inches="tight")
            saved.append(out_path)
        msg_lines = []
        if saved:
            msg_lines.append(f"已导出 {len(saved)} 张：")
            msg_lines.extend(saved)
        if skipped:
            msg_lines.append("\n跳过：")
            msg_lines.extend(skipped)
        if saved:
            messagebox.showinfo("完成", "\n".join(msg_lines))
        else:
            messagebox.showwarning("提示", "没有可导出的结果\n" + "\n".join(skipped))


class RuleDialog(tk.Toplevel):
    def __init__(self, parent, rule_dict: Optional[dict], on_save):
        super().__init__(parent)
        self.title("规则编辑")
        self.resizable(False, False)
        self.on_save = on_save
        r = rule_dict if rule_dict else asdict(Rule())
        self.vars: dict = {}

        def row(idx: int, label: str, widget):
            ttk.Label(self, text=label).grid(row=idx, column=0, padx=8, pady=4, sticky="e")
            widget.grid(row=idx, column=1, padx=8, pady=4, sticky="w")

        self.vars["name"] = tk.StringVar(value=r["name"])
        row(0, "名称：", ttk.Entry(self, textvariable=self.vars["name"], width=26))

        self.vars["frequency"] = tk.StringVar(value=r["frequency"])
        row(1, "频率：", ttk.Combobox(self, textvariable=self.vars["frequency"],
                                      values=["daily", "weekly", "monthly"],
                                      state="readonly", width=24))

        self.vars["weekday"] = tk.IntVar(value=r["weekday"])
        row(2, "星期 (0=周一..6=周日)：",
            ttk.Spinbox(self, from_=0, to=6, textvariable=self.vars["weekday"], width=24))

        self.vars["monthday"] = tk.IntVar(value=r["monthday"])
        row(3, "月日 (1-28)：",
            ttk.Spinbox(self, from_=1, to=28, textvariable=self.vars["monthday"], width=24))

        self.vars["direction"] = tk.StringVar(value=r["direction"])
        row(4, "方向 (drop=跌/rise=涨/any=不限)：",
            ttk.Combobox(self, textvariable=self.vars["direction"],
                          values=["drop", "rise", "any"], state="readonly", width=24))

        self.vars["threshold_pct"] = tk.DoubleVar(value=r["threshold_pct"])
        row(5, "阈值 (%)：", ttk.Entry(self, textvariable=self.vars["threshold_pct"], width=26))

        self.vars["action"] = tk.StringVar(value=r["action"])
        row(6, "操作 (buy=买/sell=卖)：",
            ttk.Combobox(self, textvariable=self.vars["action"],
                          values=["buy", "sell"], state="readonly", width=24))

        self.vars["amount"] = tk.DoubleVar(value=r["amount"])
        row(7, "金额 (元)：", ttk.Entry(self, textvariable=self.vars["amount"], width=26))

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=8, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="保存", command=self._save).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side="left", padx=6)

    def _save(self):
        try:
            d = {k: v.get() for k, v in self.vars.items()}
            d["threshold_pct"] = float(d["threshold_pct"])
            d["amount"] = float(d["amount"])
            d["weekday"] = int(d["weekday"])
            d["monthday"] = int(d["monthday"])
            if d["frequency"] not in ("daily", "weekly", "monthly"):
                raise ValueError("频率非法")
            if d["direction"] not in ("drop", "rise", "any"):
                raise ValueError("方向非法")
            if d["action"] not in ("buy", "sell"):
                raise ValueError("操作非法")
            self.on_save(d)
            self.destroy()
        except Exception as e:
            messagebox.showerror("错误", str(e))


if __name__ == "__main__":
    App().mainloop()
