"""Pure rules used by the incremental refactoring planner."""

from ..domain.refactoring import RefactoringTechnique

TECHNIQUES = (
    RefactoringTechnique("extract-method", "提取函数", "提取可独立命名的连续职责", ("长函数", "独立逻辑块", "输入输出边界")),
    RefactoringTechnique("inline-method", "内联函数", "消除没有表达力的间接调用", ("单行转发", "仅一个调用者")),
    RefactoringTechnique("rename-symbol", "重命名符号", "让名称准确表达业务含义", ("含糊命名", "误导命名", "动态引用")),
    RefactoringTechnique("extract-variable", "提取变量", "命名复杂表达式", ("重复表达式", "复杂条件")),
    RefactoringTechnique("inline-variable", "内联变量", "移除无意义的临时变量", ("只使用一次", "名称不增加信息")),
    RefactoringTechnique("change-function-declaration", "调整函数声明", "简化参数和返回契约", ("参数过多", "布尔参数", "未使用参数")),
    RefactoringTechnique("encapsulate-variable", "封装变量", "控制共享状态的读写入口", ("共享可变状态", "直接字段访问")),
    RefactoringTechnique("introduce-parameter-object", "引入参数对象", "聚合总是一起变化的参数", ("参数组", "重复参数组合")),
    RefactoringTechnique("combine-functions-into-class", "函数组合成类", "集中共享数据的函数", ("共享参数", "相同职责")),
    RefactoringTechnique("combine-functions-into-transform", "函数组合成变换", "集中派生数据计算", ("重复派生值", "只读变换")),
    RefactoringTechnique("split-phase", "拆分阶段", "隔离不同处理阶段", ("解析与执行混合", "计算与持久化混合")),
    RefactoringTechnique("move-function", "搬移函数", "把行为移动到最相关模块", ("跨模块依赖", "本模块依赖少")),
    RefactoringTechnique("move-field", "搬移字段", "把数据放到主要使用者中", ("字段使用集中", "跨对象访问")),
    RefactoringTechnique("replace-loop-with-pipeline", "循环改管道", "显式表达集合变换步骤", ("过滤映射聚合", "无复杂控制流")),
    RefactoringTechnique("replace-conditional-with-polymorphism", "条件改多态", "隔离随类型变化的行为", ("类型分支", "重复条件树")),
    RefactoringTechnique("decompose-conditional", "分解条件", "命名复杂条件及其分支", ("复合条件", "大分支")),
    RefactoringTechnique("consolidate-conditional-expression", "合并条件表达式", "合并结果相同的条件", ("相同结果", "分散守卫")),
    RefactoringTechnique("replace-nested-conditional-with-guard-clauses", "嵌套条件改卫语句", "降低条件嵌套", ("深层嵌套", "异常路径")),
    RefactoringTechnique("replace-magic-literal", "替换魔法值", "命名具有业务含义的字面量", ("重复字面量", "业务阈值")),
    RefactoringTechnique("separate-query-from-modifier", "查询与修改分离", "分离返回值与副作用", ("查询产生副作用", "命令返回复杂结果")),
    RefactoringTechnique("remove-flag-argument", "移除标记参数", "用明确入口代替行为开关", ("布尔参数", "参数控制大分支")),
    RefactoringTechnique("replace-derived-variable-with-query", "派生变量改查询", "消除可失效的派生状态", ("缓存派生值", "同步更新")),
    RefactoringTechnique("extract-class", "提取类", "拆出独立职责和状态", ("多重职责", "字段方法子集")),
    RefactoringTechnique("collapse-hierarchy", "折叠继承体系", "移除没有价值的继承层", ("父子类差异小", "空壳抽象")),
)

TECHNIQUE_BY_ID = {technique.id: technique for technique in TECHNIQUES}


def classify_risk(symbol_count: int, process_count: int = 0) -> str:
    if symbol_count > 15 or process_count > 5:
        return "HIGH"
    if symbol_count >= 5 or process_count >= 2:
        return "MEDIUM"
    return "LOW"
