# DataHub Grounding：Blind vs. Grounded 實證

這正是需要的對照組，而且是用 DuckDB 實際執行、拿到 runtime error
的真實證據，不是靜態分析猜測。

- **Blind 版本**：GLM 猜欄位名叫 `clv`，實際 DuckDB 執行時直接報錯
  `Binder Error: Referenced column "clv" not found in FROM clause!`，而且
  DuckDB 自己提示 `Candidate bindings: "customer_lifetime_value"`。
- **Grounded 版本**：同一個任務正確引用 `customer_lifetime_value`，不只
  跑通，還真的把 100 位顧客分成 5 組、每組 20 人。

這組對照的模型與業務任務相同，唯一差異是有沒有接上 DataHub context：
一個在 runtime 報錯，另一個一次跑對並產生查詢結果。這些結果均由
`dbt show` 對 DuckDB 實際執行取得，不是 AI 生成的執行敘述。

## 保存內容

- `task.txt`：兩個模式共用的任務描述
- `output_blind.sql`：含已知幻覺欄位 `clv` 的 SQL
- `blind_runtime_error.txt`：Blind SQL 的真實 DuckDB Binder Error
- `output_grounded.sql`：使用真實欄位的 SQL
- `grounded_runtime_output.txt`：Grounded SQL 的真實 DuckDB 結果

這組資料可以作為 `validator.py` 的第一個測試案例：

- `clv` 不在 `context_bundle.json` 欄位清單中，預期判定為 **FAIL**。
- `customer_lifetime_value` 在欄位清單中，預期判定為 **PASS**。

## 重現方式

在本 repo 根目錄執行，並讓外部 `jaffle_shop_duckdb` 位於相鄰目錄：

```powershell
$sql = Get-Content -Raw .\examples\output_blind.sql
dbt --project-dir ..\jaffle_shop_duckdb --profiles-dir ..\jaffle_shop_duckdb show --inline $sql --limit 10

$sql = Get-Content -Raw .\examples\output_grounded.sql
dbt --project-dir ..\jaffle_shop_duckdb --profiles-dir ..\jaffle_shop_duckdb show --inline $sql --limit 10
```
