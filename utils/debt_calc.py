def calculate_minimum_transactions(net_balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """
    貪婪演算法計算最少還款筆數
    :param net_balances: 字典形式 {user_id: 淨額} (大於0代表應收款，小於0代表應付款)
    :return: 包含 (欠款人_id, 收款人_id, 金額) 的列表
    """
    debtors = []
    creditors = []
    
    # 將使用者分為「欠款方 (負數)」與「收款方 (正數)」
    for uid, amount in net_balances.items():
        if amount < 0:
            debtors.append([uid, -amount]) # 轉為正數方便計算
        elif amount > 0:
            creditors.append([uid, amount])

    # 貪婪策略：由金額最大的人開始互相抵銷，能最快消化債務
    debtors.sort(key=lambda x: x[1], reverse=True)
    creditors.sort(key=lambda x: x[1], reverse=True)

    transactions = []
    i, j = 0, 0
    
    while i < len(debtors) and j < len(creditors):
        debtor_id, debt_amount = debtors[i]
        creditor_id, credit_amount = creditors[j]

        # 取兩者中較小的金額作為本次交易額
        settle_amount = min(debt_amount, credit_amount)
        transactions.append((debtor_id, creditor_id, settle_amount))

        # 扣除已結算的金額
        debtors[i][1] -= settle_amount
        creditors[j][1] -= settle_amount

        # 若該欠款人已還清，換下一個欠款人
        if debtors[i][1] == 0:
            i += 1
        # 若該收款人已收齊，換下一個收款人
        if creditors[j][1] == 0:
            j += 1

    return transactions