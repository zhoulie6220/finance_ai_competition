import * as duckdb from '@duckdb/duckdb-wasm';

// Vite 需要 ?url 后缀来正确解析 wasm 和 worker 文件路径
import duckdb_wasm from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';
import mvp_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url';
import duckdb_wasm_eh from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import eh_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url';

const MANUAL_BUNDLES: duckdb.DuckDBBundles = {
    mvp: { mainModule: duckdb_wasm, mainWorker: mvp_worker },
    eh: { mainModule: duckdb_wasm_eh, mainWorker: eh_worker },
};

let dbInstance: duckdb.AsyncDuckDB | null = null;

export async function initDuckDB(): Promise<duckdb.AsyncDuckDB> {
    if (dbInstance) return dbInstance;

    const bundle = await duckdb.selectBundle(MANUAL_BUNDLES);
    const worker = new Worker(bundle.mainWorker!);
    const logger = new duckdb.ConsoleLogger();
    const db = new duckdb.AsyncDuckDB(logger, worker);

    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    dbInstance = db;
    return db;
}

export async function queryFile(
    db: duckdb.AsyncDuckDB,
    file: File
): Promise<Record<string, unknown>[]> {
    // 1. 将文件内容注册到 DuckDB 虚拟文件系统
    const buffer = await file.arrayBuffer();
    await db.registerFileBuffer(file.name, new Uint8Array(buffer));

    // 2. 执行查询（限制 100 行）
    const conn = await db.connect();
    const result = await conn.query(`SELECT * FROM '${file.name}' LIMIT 100`);

    // 3. 将 Arrow Table 转换为普通 JS 对象数组
    const rows = result.toArray().map((row) => row.toJSON());

    await conn.close();
    return rows as Record<string, unknown>[];
}