"""facade.trace 盐外证据（archived_uid）交叉校验测试。

VERIFICATION_REPORT §9.2 残余问题 #1：facade 裸 API 多盐扫描误归因——
存档 UID 校验此前只在 CLI/find-meta（持 meta）路径生效，直接调 wm.trace
的 API 消费者无保护（19/50 触发中 13 个错误用户归因），防御是路径依赖的。

本文件验证 trace(archived_uid=...) 后：解码 UID 与存档 UID 不一致
（含掩码不对齐）→ abstain（uid=None，绝不输出可能错误的 UID），
消除路径依赖——裸 API 消费者持 meta 时传入 archived_uid 即获得
与 CLI/find-meta 等价的盐外证据防护。
"""
from __future__ import annotations

from aawm.plugins import UIDRegistry, Watermarker


# 中文长文本（约 1200+ 字，4 段不同主题技术散文），保证 zero_cost 词典
# 命中密度充足、自适应容量 ≥ 8 bit，embed/trace 稳定。
ZH_LONG = (
    "平台从每一个在编队中工作的分布式代理收集遥测数据。每个代理监视一个"
    "大事件流，保留重要变更的小记录，并在报告窗口结束时构建简短摘要。"
    "强大的监督器将结果分组为通用视图，使整个系统易于检查。当代理发现无法"
    "独自修复的困难问题时，会向中央团队发送快速警报并寻求帮助。团队然后"
    "检查问题是新是旧，是关键还是次要，以及是否可以在不完全重启服务的"
    "情况下应用快速补丁。平台还支持强大的审计跟踪，记录系统中任何代理"
    "所做的每一项重要变更，因此仔细的审查者总能找到困难问题的根本原因。"
    "常见模式是将大工作拆分为小任务，将每个任务分配给单个代理，然后将"
    "结果合并为最终报告。这种方法使系统保持稳健且易于推理，即使代理总数"
    "随时间增长且事件量成为中央团队的大挑战。"
    "风控系统在交易流水到达时执行多道校验。第一道检查账户的余额是否充足，"
    "第二道核对收款方的身份标识是否与历史记录一致，第三道评估交易金额是否"
    "偏离该账户的常规模式。任何一道校验失败都会触发人工复核队列，复核员"
    "可以在三十分钟内做出冻结或者放行的决定。系统还会定期把异常交易的"
    "特征向量写入共享图谱，用于发现跨账户的洗钱环路。图谱构建依赖每天"
    "凌晨的低峰窗口，全量重算一次，通常需要两个小时的算力。风控团队依据"
    "图谱的连通性评分调整规则阈值，把误伤率控制在万分之三以内，同时保留"
    "对新型诈骗模式的快速响应能力。"
    "物流调度中心每天处理数万条订单，需要把每一件货物分配到合适的车辆。"
    "调度算法首先按收货地址聚类出区域簇，再根据车辆载重和容积约束求解"
    "路径。实际运营中经常遇到临时取消订单、道路施工和天气预警，调度系统"
    "必须在一个可接受的时间内给出次优解，而不是等待全局最优解。司机端"
    "应用实时展示任务变更，同时把行驶轨迹回传中心，用于事后核对里程与"
    "油耗。中心的监控大屏按线路统计准点率，一旦某条线路连续三天低于"
    "基准值，就会触发重新分区的评估流程。调度员还可以手动锁定某辆车，"
    "优先处理医院的紧急物资需求。"
    "医疗信息系统把门诊病历、检验报告和影像资料统一归档，为医生提供"
    "全病程视图。病历摘要由系统自动生成，提取主诉、现病史、既往史和"
    "过敏信息，并附上最近的化验趋势图。医生在开立处方时，系统会检查"
    "药物之间的相互作用，对高风险组合给出醒目提示。影像科的报告模板"
    "按部位分型，胸片、腹部和骨科各有独立的描述规范，避免遗漏关键"
    "征象。系统还提供患者随访提醒，在出院后的第七天自动发送复诊问卷，"
    "收集不良事件数据并汇入质控看板。质控委员会每月评审一次误诊案例，"
    "把经验沉淀为新的检查清单，推动临床路径的持续改进。"
    "智能合约在区块链网络中得到广泛部署，其核心价值是让多方在互不信任"
    "的前提下自动执行约定条款。合约代码一旦发布就难以修改，因此审计"
    "环节尤为关键。审计团队会逐行检查资金转移函数的边界条件，确认没有"
    "整数溢出或重入攻击的隐患。部署流程分为测试网验证和主网发布两个"
    "阶段，每个阶段都要求至少两名成员签名确认。链上事件会实时同步到"
    "监控看板，任何异常的大额转账都会触发延迟结算，等待人工复核。合约"
    "升级采用代理模式，把业务逻辑与存储状态分离，从而在必要时可以"
    "平滑替换实现而不丢失历史数据。社区治理通过提案投票推进，每个"
    "提案的链上执行结果都会形成可追溯的审计日志。"
    "数据分析平台每天接收海量日志，从原始记录中提取用户行为特征。"
    "采集层负责清洗和标准化字段，过滤掉爬虫流量和无效请求。特征工程"
    "模块把会话切分为行为序列，计算访问频次、停留时长和转化漏斗。"
    "模型训练使用增量学习，每天凌晨用前一天的样本刷新参数，同时保留"
    "七天内的历史版本以便回滚。实验平台支持在线对比测试，把流量按"
    "哈希分桶分配到不同策略，实时比较各项业务指标。结果报表按小时"
    "汇总，异常波动会自动标注并推送到值班群。数据仓库采用分层建模，"
    "明细层、汇总层和应用层相互隔离，权限控制严格区分读写账号。"
    "每次模型发布都会记录特征依赖和训练配置，确保效果评估可以被"
    "完全复现。"
)


def _load_corpus():
    """仓库 docs/*.md 中文技术散文语料（标定口径，与验证报告同源）。"""
    import glob
    corpus = []
    for p in glob.glob("docs/*.md"):
        try:
            corpus.append(open(p, encoding="utf-8").read())
        except OSError:
            continue
    return corpus


def _wm() -> Watermarker:
    """中文 zero_cost Watermarker（docs 语料标定，与报告 §9.2 标定口径一致）。"""
    return Watermarker(language="zh", codec_mode="zero_cost",
                       calibrate_corpus=_load_corpus())


def _embed_uid_7():
    """嵌入一份 1200+ 字中文文本到 UID=7，返回 (wm, result)。"""
    wm = _wm()
    result = wm.embed(ZH_LONG, user_id=7)
    assert result.watermarked_text != ZH_LONG
    return wm, result


# ----------------------------------------------------------------------
# _uid_alias_match 静态方法单元测试
# ----------------------------------------------------------------------

class TestUidAliasMatch:
    def test_exact_match(self):
        assert Watermarker._uid_alias_match(7, 7, 8) is True

    def test_mask_alias_match(self):
        """自适应 k-bit 语义：存档 UID 高位置 1，低 n_bits 位与解码一致 → 一致。"""
        assert Watermarker._uid_alias_match(0x34, 0x1234, 8) is True

    def test_mask_alias_match_str(self):
        """meta 存档 UID 常为数字字符串，须可转换（十进制数字串）。"""
        assert Watermarker._uid_alias_match(0x34, "4660", 8) is True  # 0x1234 的十进制

    def test_distortion_mismatch(self):
        assert Watermarker._uid_alias_match(0, 17, 8) is False

    def test_mask_flip_mismatch(self):
        """掩码对齐不是放水：低 n_bits 位不同 → 不一致。"""
        assert Watermarker._uid_alias_match(7, 8, 8) is False
        assert Watermarker._uid_alias_match(7, 0x0108, 8) is False

    def test_none_uid_returns_false(self):
        assert Watermarker._uid_alias_match(None, 7, 8) is False

    def test_none_archived_returns_false(self):
        assert Watermarker._uid_alias_match(7, None, 8) is False

    def test_non_adaptive_zero_bits_exact_only(self):
        """非自适应路径 n_bits=0 → 只认精确相等，掩码比对退化为 None。"""
        assert Watermarker._uid_alias_match(7, 7, 0) is True
        assert Watermarker._uid_alias_match(7, 0x0107, 0) is False


# ----------------------------------------------------------------------
# facade.trace(archived_uid=...) 集成测试
# ----------------------------------------------------------------------

class TestTraceArchivedUid:
    def test_archived_uid_match_keeps_attribution(self):
        """正确盐 + archived_uid=user_id → 归因保留（不 abstain，uid 解码正确）。"""
        wm, result = _embed_uid_7()
        t = wm.trace(
            result.watermarked_text,
            session_salt=result.session_salt,
            bands=result.bands,
            n_bits=result.n_bits,
            archived_uid=result.user_id,
        )
        assert t.watermarked is True
        assert t.attribution_abstain is False
        assert t.uid is not None
        # 解码值应与存档 UID 掩码对齐（k-bit 空间）
        mask = (1 << result.n_bits) - 1 if result.n_bits else None
        assert (t.uid == result.user_id) or (mask and t.uid == (result.user_id & mask))

    def test_archived_uid_distortion_abstains(self):
        """正确盐但 archived_uid 失真（低 k 位不同）→ abstain，uid=None。
        这正是报告 §9.2 的"13 个错误用户归因"场景——现在转为干净弃权。"""
        wm, result = _embed_uid_7()
        t = wm.trace(
            result.watermarked_text,
            session_salt=result.session_salt,
            bands=result.bands,
            n_bits=result.n_bits,
            archived_uid=result.user_id ^ 1,  # 翻转最低位 → 低 n_bits 位必不同
        )
        assert t.watermarked is True
        assert t.attribution_abstain is True
        assert t.uid is None
        assert t.user is None

    def test_archived_uid_mask_alias_ok(self):
        """存档 UID 高位不同但低 n_bits 位一致 → 视为同一用户，归因保留。"""
        wm, result = _embed_uid_7()
        auid = result.user_id + (1 << result.n_bits)  # 高位置 1 的"别名"UID
        t = wm.trace(
            result.watermarked_text,
            session_salt=result.session_salt,
            bands=result.bands,
            n_bits=result.n_bits,
            archived_uid=auid,
        )
        assert t.watermarked is True
        assert t.attribution_abstain is False
        assert t.uid is not None

    def test_without_archived_uid_unchanged(self):
        """不传 archived_uid → 行为不变（向后兼容护栏）。"""
        wm, result = _embed_uid_7()
        t = wm.trace(
            result.watermarked_text,
            session_salt=result.session_salt,
            bands=result.bands,
            n_bits=result.n_bits,
        )
        assert t.watermarked is True
        assert t.uid is not None

    def test_multi_salt_scan_never_wrong_uid(self):
        """多盐扫描（档案扫描）护栏：对候选盐逐一 trace 且均传 archived_uid=真值，
        断言"绝无错误 UID 归因"——每个盐要么未检出、要么 abstain、
        要么解码与真值掩码对齐。错误盐"检出+错误 UID 自信归因"被消灭。
        """
        import os
        wm, result = _embed_uid_7()
        mask = (1 << result.n_bits) - 1 if result.n_bits else None
        cand_salts = [result.session_salt] + [os.urandom(16) for _ in range(5)]
        for i, salt in enumerate(cand_salts):
            t = wm.trace(
                result.watermarked_text,
                session_salt=salt,
                # 只有正确盐持有 bands 元数据（真实运营：meta 与盐一一对应）
                bands=result.bands if i == 0 else None,
                n_bits=result.n_bits if i == 0 else None,
                archived_uid=7,
            )
            if t.watermarked and t.uid is not None:
                # 任何给出归因的盐，解码 UID 必须与存档 UID 掩码对齐
                assert (t.uid == 7) or (mask and t.uid == (7 & mask)), (
                    f"salt#{i} 归因到错误 UID=0x{t.uid:04X}（真值 7）")
        # 正确盐必须检出且归因正确
        t0 = wm.trace(
            result.watermarked_text,
            session_salt=result.session_salt,
            bands=result.bands,
            n_bits=result.n_bits,
            archived_uid=7,
        )
        assert t0.watermarked is True
        assert t0.uid is not None

    def test_multi_salt_scan_with_registry_no_wrong_user(self):
        """带注册库的多盐扫描：错误盐即使检出并解码出其他注册用户，
        传 archived_uid 后 user 必须 abstain（None），不得输出错误用户。"""
        import os
        reg = UIDRegistry()
        for u in (7, 104, 117, 123, 100):
            reg.register(f"user{u}", uid=u)
        wm = Watermarker(language="zh", codec_mode="zero_cost", registry=reg)
        result = wm.embed(ZH_LONG, user_id=7)
        for _ in range(6):
            t = wm.trace(
                result.watermarked_text,
                session_salt=os.urandom(16),  # 错误盐（无 bands）
                archived_uid=7,
            )
            # 错误盐场景：存在性可能存活（盐无关），但归因要么 abstain
            # 要么锁定 user7——绝不输出 user104/117 等错误用户
            if t.watermarked:
                assert t.attribution_abstain or t.user == "user7", (
                    f"错误盐归因到 {t.user}（真值 user7，abstain={t.attribution_abstain}）")
