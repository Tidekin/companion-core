from companion import paid_budget


def rehash(plan):
    plan["plan_sha256"] = paid_budget.sha({k: v for k, v in plan.items() if k != "plan_sha256"})
