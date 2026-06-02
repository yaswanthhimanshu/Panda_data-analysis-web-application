# Panda - No-Code Pandas Workbench

Live project: Add Render live link after deployment

Panda is a no-code pandas workbench for exploring CSV and Excel datasets through an interactive browser interface. I built it to make common pandas analysis workflows easier to run, inspect, explain, visualize, and export without manually writing code for every operation.

The main goal of Panda is practical dataset exploration. A user can upload a file, understand the structure of the data, run statistical and group-based analysis, generate charts, inspect pandas-style code, and export the results.

---

## What I Built

I built an interactive pandas analysis system that turns common DataFrame operations into guided UI workflows.

The app supports:

- Uploading CSV, XLSX, and XLS datasets.
- Detecting CSV encoding automatically.
- Keeping each user's uploaded dataset isolated in a private browser session.
- Running pandas-based analysis from UI controls.
- Showing tables, charts, metrics, and pandas code explanations.
- Exporting analysis results to CSV or Excel.

The interface is built around analysis sections instead of one large form. Each section focuses on a specific type of question, such as:

- What does this dataset contain?
- Which columns have missing values?
- What are the summary statistics?
- Which groups perform better or worse?
- Which variables are correlated?
- Are there outliers?
- What are the top or bottom records?

---

## Why I Built It

When working with tabular datasets, the same exploration steps are repeated again and again:

- Load the file.
- Check rows, columns, data types, and missing values.
- Preview records.
- Run `describe()`.
- Check value counts.
- Group by categories.
- Create pivot tables.
- Look at correlations.
- Detect outliers.
- Export important results.

These steps are simple in pandas, but they still require writing repeated code and remembering the exact syntax. Panda brings those workflows into one place so the user can focus on analysis decisions instead of boilerplate code.

I also wanted the app to show the reasoning behind each operation. Many sections include a pandas code preview so the user can connect the UI action with the actual pandas operation behind it.

---

## Core Workflow

1. **Upload a dataset**
   - The user uploads a CSV, XLSX, or XLS file.
   - CSV encoding is detected automatically.
   - The file is read into a pandas DataFrame.
   - The dataset is stored temporarily in memory for that browser session.

2. **Review dataset structure**
   - The overview section shows row count, column count, missing values, memory usage, and column details.
   - This gives the user a first understanding of the dataset before deeper analysis.

3. **Inspect raw records**
   - The preview section lets the user inspect head, tail, sample, and selected rows/columns.
   - This helps verify that the file loaded correctly and that values look reasonable.

4. **Choose an analysis operation**
   - The sidebar organizes analysis into overview, statistics, categorical analysis, group analysis, relationships, distributions, and data tools.
   - Each operation has its own parameter controls.

5. **Run analysis**
   - The backend applies the selected pandas operation.
   - The result is returned as a table, chart, metric summary, or matrix depending on the operation.

6. **Read the pandas equivalent**
   - Many outputs include a code preview showing the pandas logic used for that analysis.
   - This makes the workflow explainable instead of being a black box.

7. **Export results**
   - Analysis result tables can be exported as CSV or Excel files.
   - This supports the upload-analyze-export workflow.

---

## Features Implemented

### 1. File Upload and Encoding Detection

Panda supports CSV, XLSX, and XLS uploads. For CSV files, the backend checks the file encoding before reading it with pandas.

Why I built it:

- Real CSV files often fail because of encoding issues.
- A user should not need to know whether a file is UTF-8, Latin-1, or another encoding before upload.
- Supporting Excel files makes the app useful for common business datasets.

---

### 2. Private Session-Based Dataset Handling

Each browser session receives a private session ID. Uploaded datasets are stored in memory using that session ID.

```python
_STORE[session_id] = {
    "df": dataframe,
    "filename": filename,
    "encoding": encoding,
    "last_access": timestamp
}
```

Why I built it:

- User A should not see User B's dataset.
- Uploaded files should not be saved permanently to server disk.
- The app is designed for temporary analysis, not long-term file storage.
- Memory-based storage works well for an upload-analyze-export workflow.

Inactive datasets are removed after 2 hours of inactivity so server memory does not keep growing forever.

---

### 3. Dataset Overview

The overview section summarizes the dataset at a high level.

Implemented output:

- Total rows.
- Total columns.
- Missing value percentage.
- Memory usage.
- Numeric column count.
- Text/object column count.
- Per-column data type, unique count, missing count, and missing percentage.

Why I built it:

- Every analysis should start with understanding the shape and quality of the dataset.
- Missing values and data types affect which analysis methods make sense.
- Column-level metadata helps decide what to inspect next.

---

### 4. Column Profiler

The column profiler analyzes one selected column in detail.

For numeric columns, it can show:

- Mean.
- Median.
- Mode.
- Standard deviation.
- Variance.
- Min and max.
- Q1, Q3, and IQR.
- Skewness and kurtosis.
- Histogram or distribution chart.

For categorical columns, it can show:

- Unique count.
- Missing values.
- Top values.
- Value-count chart.

Why I built it:

- A single column can have issues that are not obvious from the full dataset.
- Numeric columns need distribution checks before deeper statistics.
- Categorical columns need frequency checks to find dominant or rare values.

---

### 5. Dataset Preview

The preview feature lets users inspect actual rows from the dataset.

Implemented behavior:

- Preview selected columns.
- View a limited number of rows.
- Apply row-range selection.
- Check raw values before analysis.

Why I built it:

- Summary statistics are not enough.
- Users need to see real records to understand context, formatting, and possible data issues.
- Previewing protects against analyzing a wrongly loaded file.

---

### 6. Statistical Summary

The statistical summary uses pandas-style descriptive statistics for numeric columns.

Implemented output:

- Count.
- Mean.
- Standard deviation.
- Minimum.
- 25th percentile.
- Median.
- 75th percentile.
- Maximum.

Why I built it:

- `df.describe()` is one of the first pandas commands used during exploration.
- It quickly reveals scale, spread, and possible unusual values.
- It gives a compact numeric summary before deeper analysis.

---

### 7. Central Tendency

This section calculates mean, median, and mode for numeric columns.

Why I built it:

- Mean and median comparison helps identify skew.
- Mode shows the most common value.
- These metrics are simple but important for understanding a column's center.

---

### 8. Standard Deviation and Variance

This section measures spread and variability.

Implemented output:

- Mean.
- Standard deviation.
- Variance.
- Coefficient of variation percentage.
- Spread rating.

Why I built it:

- Standard deviation shows how much values vary around the mean.
- Variance is useful for statistical understanding.
- Coefficient of variation helps compare spread between columns with different scales.

---

### 9. Skewness and Kurtosis

This section checks the shape of numeric distributions.

Why I built it:

- Skewness helps identify whether data has a long left or right tail.
- Kurtosis helps identify heavy-tailed or flat distributions.
- Distribution shape matters before modeling, statistical testing, or threshold decisions.

---

### 10. Quantile Analysis

Quantile analysis shows percentile values for a selected numeric column.

Implemented output:

- Common percentiles.
- Q1.
- Median.
- Q3.
- IQR.
- Percentile chart.

Why I built it:

- Percentiles explain distribution without assuming normality.
- They help detect extreme values and decide meaningful cutoffs.
- IQR is also used for outlier detection.

---

### 11. Value Counts

Value counts show how often each category or value appears.

Why I built it:

- Categorical analysis starts with frequency.
- It helps find dominant classes, rare values, spelling inconsistencies, and imbalance.
- It is useful for columns like department, region, product, status, or category.

---

### 12. Frequency Analysis

Frequency analysis extends value counts with percentages and cumulative percentages.

Why I built it:

- Counts alone do not show proportion clearly.
- Percentages make categories easier to compare.
- Cumulative percentage helps identify which categories make up most of the data.

---

### 13. Group Analysis Studio

Group Analysis Studio is one of the main features. It lets users group by one or more columns and calculate metrics for each group.

Implemented calculations:

- Mean.
- Sum.
- Count.
- Min.
- Max.
- Median.
- Standard deviation.
- Variance.
- Mode.

Why I built it:

- Grouping is one of the most important pandas workflows.
- Users often need to compare salary by department, sales by region, churn by segment, or revenue by product.
- Supporting multiple group columns allows deeper segmentation.
- Supporting multiple calculations makes the output more useful than a single aggregation.

---

### 14. Pivot Table

The pivot table feature reshapes data into a matrix using row categories, column categories, values, and aggregation.

Why I built it:

- Pivot tables are useful for comparing two dimensions at once.
- They make grouped summaries easier to scan.
- This mirrors a common Excel and pandas workflow.

---

### 15. Crosstab

Crosstab compares two categorical columns and can optionally normalize values.

Why I built it:

- It helps understand relationships between categories.
- Normalized crosstabs make proportions easier to interpret.
- This is useful for demographic, survey, customer, or classification-style datasets.

---

### 16. Correlation Studio

Correlation Studio calculates correlations between numeric columns and displays a heatmap.

Implemented behavior:

- Pearson, Spearman, or Kendall correlation.
- Correlation heatmap.
- Top positive correlation pairs.
- Top negative correlation pairs.

Why I built it:

- Correlation helps identify variables that move together.
- It can reveal multicollinearity before modeling.
- Heatmaps make relationships easier to inspect visually.

---

### 17. Covariance

The covariance section calculates a covariance matrix and ranks covariance pairs.

Why I built it:

- Covariance is a lower-level relationship measure behind correlation.
- It shows whether two variables tend to move in the same or opposite direction.
- It is useful when scale matters.

---

### 18. Ranking Analysis

Ranking Analysis shows the largest and smallest records for a selected column.

Why I built it:

- Analysts often need to find top customers, highest salaries, lowest ratings, best products, or worst performers.
- Showing both top and bottom records gives quick comparison.
- It avoids manually sorting and scanning the full dataset.

---

### 19. Distribution Analysis

Distribution Analysis visualizes the shape of a numeric column.

Implemented output:

- Histogram.
- Mean, median, and mode.
- Standard deviation.
- Skewness and kurtosis labels.

Why I built it:

- Averages alone can be misleading.
- Distribution shape shows whether the data is normal, skewed, concentrated, or spread out.
- Visual inspection is important before using statistical assumptions.

---

### 20. Outlier Detection

Outlier detection uses the IQR method.

Implemented output:

- Q1.
- Q3.
- IQR.
- Lower fence.
- Upper fence.
- Outlier count.
- Outlier percentage.
- Boxplot.

Why I built it:

- Outliers can be data errors or meaningful extreme cases.
- IQR is a common and explainable method.
- The output shows the threshold values instead of only saying that outliers exist.

---

### 21. Scatter Plot

The scatter plot feature compares two numeric columns visually.

Implemented output:

- Scatter chart.
- Trend line.
- Pearson correlation coefficient.

Why I built it:

- Some relationships are easier to see visually than in a table.
- Scatter plots can show clusters, linear patterns, non-linear shapes, and unusual points.
- The trend line gives quick directional context.

---

### 22. Sort Studio

Sort Studio orders records by a selected column.

Why I built it:

- Sorting is useful when the user wants raw record-level ranking instead of aggregated ranking.
- It helps inspect highest, lowest, newest, oldest, or alphabetically ordered rows.

---

### 23. Data Slicer

Data Slicer filters rows using conditions.

Implemented filters:

- Equal.
- Not equal.
- Contains.
- In list.
- Greater than.
- Less than.
- Greater than or equal.
- Less than or equal.

Why I built it:

- Users often need to analyze a subset of the dataset.
- Filtering before analysis makes results more specific.
- This supports questions like "only churned customers," "only high-value orders," or "only one region."

---

### 24. Export to CSV and Excel

Result tables can be exported.

Why I built it:

- Analysis does not end inside the app.
- Users may need to share results, continue work in Excel, or save outputs.
- Exporting makes the workflow complete: upload, analyze, export.

---

### 25. Pandas Code Preview

Many analysis outputs include the pandas equivalent code.

Why I built it:

- The app should be explainable.
- Users can understand what operation was performed.
- It connects UI actions with real pandas syntax.
- It makes the tool useful for both analysis and learning pandas workflows.

---

## What Makes Panda Different

Panda is not just a file previewer. It combines multiple common pandas workflows into one connected analysis workspace.

The workflow is designed around:

- Fast dataset understanding.
- Practical statistical summaries.
- Grouped and segmented analysis.
- Relationship discovery.
- Distribution and outlier inspection.
- Temporary private sessions.
- Exportable outputs.
- Transparent pandas code previews.

The main idea is to make pandas exploration faster while still keeping the logic visible.

---

## Implementation

- **Data processing**: pandas, NumPy
- **Visualization**: Matplotlib, Seaborn
- **Backend**: Flask
- **Frontend**: HTML, CSS, JavaScript
- **File support**: CSV, XLSX, XLS
- **Export support**: CSV and Excel
- **Session handling**: private in-memory dataset store with inactivity cleanup
- **Deployment**: Render

---

## Notes

- Uploaded datasets are temporary and stored only in RAM.
- The app does not create a permanent upload folder.
- The public demo runs on a free Render instance, so very large datasets or heavy chart operations may hit memory limits.
- Users should export results before leaving the app.
- If the server restarts or redeploys, uploaded datasets are cleared.
- Inactive datasets are removed after 2 hours.

---

## Developer

Built by Yaswanth Himanshu.

GitHub: https://github.com/YaswanthHimanshu
