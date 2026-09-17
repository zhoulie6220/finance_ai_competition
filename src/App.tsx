import { useState, useEffect, useCallback } from 'react';
import { useDropzone } from 'react-dropzone';
import { initDuckDB, queryFile } from './db';
import type * as duckdb from '@duckdb/duckdb-wasm';

function App() {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [db, setDb] = useState<duckdb.AsyncDuckDB | null>(null);

  // 初始化 DuckDB
  useEffect(() => {
    initDuckDB()
      .then(setDb)
      .catch((e) => setError(e.message));
  }, []);

  const onDrop = useCallback(
    async (acceptedFiles: File[]) => {
      if (!db || acceptedFiles.length === 0) return;
      setLoading(true);
      setError(null);
      try {
        const result = await queryFile(db, acceptedFiles[0]);
        setRows(result);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : '查询失败');
      } finally {
        setLoading(false);
      }
    },
    [db]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { 'text/csv': ['.csv'] },
  });

  const columns = rows.length > 0 ? Object.keys(rows[0]) : [];

  return (
    <div style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>财报分析 MVP</h1>

      <div
        {...getRootProps()}
        style={{
          border: '2px dashed #aaa',
          borderRadius: 8,
          padding: 40,
          textAlign: 'center',
          cursor: 'pointer',
          background: isDragActive ? '#eef' : '#fafafa',
          marginBottom: 24,
        }}
      >
        <input {...getInputProps()} />
        {isDragActive
          ? '松开鼠标放入文件'
          : '拖入 CSV 文件到此处，或点击选择文件'}
      </div>

      {loading && <p>正在查询...</p>}
      {error && <p style={{ color: 'red' }}>{error}</p>}

      {rows.length > 0 && (
        <table border={1} cellPadding={8} style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col} style={{ background: '#f0f0f0' }}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {columns.map((col) => (
                  <td key={col}>{String(row[col] ?? '')}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {rows.length > 0 && (
        <p style={{ marginTop: 8, color: '#666' }}>
          共显示 {rows.length} 行数据
        </p>
      )}
    </div>
  );
}

export default App;