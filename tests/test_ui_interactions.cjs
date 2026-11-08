const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function uiContext() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            id, value: '', innerHTML: '', textContent: '', checked: false, disabled: false,
            scrollTop: 0, hidden: false, dataset: {}, open: false,
            addEventListener() {}, querySelectorAll() {return [];},
            showModal() {this.open = true;}, close() {this.open = false;},
            checkValidity() {return true;}, reportValidity() {return true;}, focus() {},
        });
        return elements.get(id);
    };
    const document = {
        getElementById: element,
        querySelectorAll() {return [];},
        querySelector() {return {content:'test'};},
    };
    const context = vm.createContext({document, Headers, CSS:{escape:value=>value}, confirm:()=>true});
    const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
    const source = html.split('<script>')[1].split('</script>')[0].replace(/loadWorkspace\(\);\s*$/, '');
    vm.runInContext(source, context);
    return {context, element, run: code => vm.runInContext(code, context)};
}

test('reset pending is visible alongside the last successful sync', () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'客户库'}],jobs:[{
            id:'job',source_id:'a',schema:'dbo',table:'Orders',name:'订单',enabled:true,
            columns:[{source:'Id'}],incremental:{enabled:true,column:'Id'},target:{table_id:'tbl'}
        }]};
        store.states={job:{last_success_at:'2026-11-09',reset_pending:true}};
        renderJobs();
    `);
    assert.match(ui.element('job-list').innerHTML, /待全量重扫/);
    assert.match(ui.element('job-list').innerHTML, /上次成功 2026-11-09/);
    ui.run('delete store.states.job.reset_pending; renderJobs();');
    assert.doesNotMatch(ui.element('job-list').innerHTML, /待全量重扫/);
});

test('selection remains global across pages and multiple-name search', () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'客户库'}],jobs:[]};
        store.tables=Array.from({length:210},(_,index)=>({source_id:'a',schema:'dbo',name:'Table'+index}));
        $('builder-source').value='a';
        restoreTablePlan();
        renderTables();
    `);
    assert.equal((ui.element('table-list').innerHTML.match(/class="table-row/g)||[]).length, 20);
    ui.element('table-next').onclick();
    assert.match(ui.element('table-page-info').textContent, /第 2\/11 页/);
    assert.equal(ui.run('pendingTables.size'), 210);
    ui.element('table-search').value='Table100,Table209';
    ui.element('table-search').oninput();
    assert.equal(ui.run('visibleTables().length'), 2);
    ui.element('clear-visible-tables').onclick();
    assert.equal(ui.run('pendingTables.size'), 208);
    assert.equal(ui.run('pendingTables.has(tableKey("a","dbo","Table100"))'), false);
    ui.element('table-search').value='';
    ui.element('table-search').oninput();
    assert.equal(ui.run('pendingTables.size'), 208);
});

test('saving drawer preserves order, page and pending table selections', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'客户库'}],jobs:Array.from({length:45},(_,i)=>({
            id:'job'+i,source_id:'a',schema:'dbo',table:'Table'+i,name:'Task'+i,enabled:true,
            columns:[{source:'Id',target:'Id',sql_type:'int'}],unique_key:'Id',
            incremental:{enabled:false,column:''},target:{mode:'auto',table_id:'',table_name:'Target'+i,auto_create_fields:true}
        }))};
        store.tables=[];
        listPages.job=2;
        renderAll=()=>renderJobs();
        selectedColumns=()=>[{source:'Id',target:'Id',sql_type:'int'}];
        $('job-id').value='job25';$('job-source-id').value='a';
        $('job-schema').value='dbo';$('job-table').value='Table25';
        $('job-name').value='编辑后的任务';$('job-enabled').checked=true;
        $('unique-key').value='Id';$('split-row-limit').value='40000';
        $('target-mode').value='auto';$('target-table-name').value='Target25';
        $('job-editor-dialog').open=true;
        api=async(url, options)=>({ok:true,config:{...store.config,jobs:store.config.jobs.map(job=>job.id===JSON.parse(options.body).job.id?JSON.parse(options.body).job:job)}});
    `);
    await ui.element('save-job').onclick();
    assert.equal(ui.run('store.config.jobs[25].id'), 'job25');
    assert.equal(ui.run('store.config.jobs[25].name'), '编辑后的任务');
    assert.equal(ui.run('listPages.job'), 2);
    assert.equal(ui.element('job-editor-dialog').open, false);
});

test('failed drawer save retains input and saved configuration for retry', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[],jobs:[{id:'job',target:{table_id:''},name:'原任务'}]};
        selectedColumns=()=>[{source:'Id',target:'Id',sql_type:'int'}];
        $('job-id').value='job';$('job-source-id').value='a';$('job-name').value='未保存的修改';
        $('unique-key').value='Id';$('target-mode').value='auto';
        $('split-row-limit').value='40000';$('job-editor-dialog').open=true;
        api=async()=>{throw new Error('network disconnected');};
    `);
    await ui.element('save-job').onclick();
    assert.equal(ui.element('job-name').value, '未保存的修改');
    assert.equal(ui.run('store.config.jobs[0].name'), '原任务');
    assert.equal(ui.element('job-editor-dialog').open, true);
    assert.equal(ui.element('save-job').disabled, false);
    assert.equal(ui.element('job-editor-fields').disabled, false);
    assert.match(ui.element('job-message').textContent, /修改已保留/);
});


test('opening an excluded table for editing keeps the current plan selection', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'库'}],jobs:[{id:'job',source_id:'a',schema:'dbo',table:'Orders',
            enabled:true,columns:[{source:'Id',target:'Id',sql_type:'int'}],unique_key:'Id',
            incremental:{enabled:false},target:{mode:'auto',table_name:'Orders'}}]};
        store.tables=[{source_id:'a',schema:'dbo',name:'Orders'}];
        pendingTables.clear();
        renderColumns=()=>{};
        api=async()=>({ok:true,columns:[{name:'Id',sql_type:'int'}],primary_key:['Id']});
    `);
    await ui.run('openJobEditor("a","dbo","Orders")');
    assert.equal(ui.element('job-enabled').checked, false);
    assert.equal(ui.element('job-editor-dialog').open, true);
});

test('batch incremental uses all filtered pages and keeps unsaved table choices', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'库'}],jobs:Array.from({length:45},(_,i)=>({
            id:'job'+i,source_id:'a',schema:'dbo',table:'Orders'+i,name:'Orders'+i,enabled:true,
            columns:[{source:'Id',sql_type:'int'}],incremental:{enabled:false,column:''},target:{table_id:''}
        }))};
        $('job-search').value='Orders';$('job-status-filter').value='all';
        listPages.job=2;
        pendingTables.set('unsaved',true);
        renderAll=()=>renderJobs();
        api=async(url, options)=>{
            globalThis.savedBatch=JSON.parse(options.body);
            return {ok:true,config:store.config,updated:Array(45).fill({}),skipped:[]};
        };
        renderJobs();
    `);
    ui.element('configure-incremental').onclick();
    assert.equal(ui.run('incrementalBatchIds.length'), 45);
    assert.equal(ui.element('incremental-dialog').open, true);
    ui.element('incremental-batch-mode').value='auto';
    await ui.element('save-incremental-batch').onclick();
    assert.equal(ui.run('savedBatch.job_ids.length'), 45);
    assert.equal(ui.run('listPages.job'), 2);
    assert.equal(ui.run('pendingTables.has("unsaved")'), true);
    assert.match(ui.element('incremental-batch-results').textContent, /已配置 45 个任务/);
});

test('filtered sync and new-only sync submit exact enabled scopes', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[{id:'a',name:'库'}],jobs:[
            {id:'old',name:'Orders old',schema:'dbo',table:'Old',source_id:'a',enabled:true},
            {id:'new',name:'Orders new',schema:'dbo',table:'New',source_id:'a',enabled:true},
            {id:'off',name:'Orders off',schema:'dbo',table:'Off',source_id:'a',enabled:false}
        ]};
        store.states={old:{last_success_at:'2026-11-04'}};
        $('job-search').value='Orders';$('job-status-filter').value='all';
        globalThis.calls=[];
        startRun=async(dry,id,ids)=>calls.push({dry,id,ids});
    `);
    await ui.element('run-filtered-jobs').onclick();
    await ui.element('run-new-jobs').onclick();
    assert.deepEqual(JSON.parse(ui.run('JSON.stringify(calls[0].ids)')), ['old','new']);
    assert.deepEqual(JSON.parse(ui.run('JSON.stringify(calls[1].ids)')), ['new']);
});

test('batch failure keeps dialog and selection for retry', async () => {
    const ui = uiContext();
    ui.run(`
        store.config={sources:[],jobs:[]};
        incrementalBatchIds=['job'];
        $('incremental-dialog').open=true;
        $('incremental-batch-mode').value='column';$('incremental-batch-column').value='UpdatedAt';
        api=async()=>{throw new Error('network disconnected');};
    `);
    await ui.element('save-incremental-batch').onclick();
    assert.equal(ui.element('incremental-dialog').open, true);
    assert.equal(ui.element('incremental-batch-column').value, 'UpdatedAt');
    assert.equal(ui.element('save-incremental-batch').disabled, false);
    assert.equal(ui.run('incrementalBatchIds[0]'), 'job');
    assert.match(ui.element('incremental-batch-results').textContent, /network disconnected/);
});

test('missing incremental field blocks save and keeps the editor open', async () => {
    const ui = uiContext();
    ui.run(`
        $('job-source-id').value='a';$('unique-key').value='Id';
        $('incremental-enabled').checked=true;$('incremental-column').value='';
        $('job-editor-dialog').open=true;
        api=async()=>{throw new Error('must not submit');};
    `);
    await ui.element('save-job').onclick();
    assert.match(ui.element('job-message').textContent, /请先选择增量字段/);
    assert.equal(ui.element('job-editor-dialog').open, true);
    ui.element('incremental-enabled').checked=false;
    ui.element('incremental-enabled').onchange();
    assert.equal(ui.element('job-message').textContent, '');
    assert.equal(ui.element('incremental-column-wrap').hidden, true);
});

test('database type changes defaults and hides network fields for SQLite', () => {
    const ui = uiContext();
    ui.run('editSource()');
    ui.element('source-type').value='postgresql';
    ui.element('source-type').onchange();
    assert.equal(ui.element('source-port').value, 5432);
    assert.equal(ui.run('sourcePayload().type'), 'postgresql');
    ui.element('source-type').value='sqlite';
    ui.element('source-type').onchange();
    assert.equal(ui.element('source-server-wrap').hidden, true);
    assert.equal(ui.element('source-password-wrap').hidden, true);
    assert.match(ui.element('source-database-label').textContent, /绝对路径/);
    ui.run("editSource({id:'legacy',port:1433})");
    assert.equal(ui.element('source-type').value, 'mssql');
    assert.equal(ui.element('source-server-wrap').hidden, false);
});

test('TLS editor restores saved settings and only submits applicable certificate fields', () => {
    const ui = uiContext();
    ui.run(`editSource({id:'pg',type:'postgresql',tls:{mode:'verify-full',ca:'/certs/ca.pem',cert:'/certs/client.pem',key:'/certs/key.pem'}})`);
    assert.equal(ui.element('source-tls-ca-wrap').hidden, false);
    assert.equal(ui.run('sourcePayload().tls.ca'), '/certs/ca.pem');
    ui.element('source-tls-mode').value='require';
    ui.element('source-tls-mode').onchange();
    assert.equal(ui.element('source-tls-ca-wrap').hidden, true);
    assert.equal(ui.run('sourcePayload().tls.ca'), '');
    assert.equal(ui.run('sourcePayload().tls.cert'), '/certs/client.pem');
    ui.element('source-tls-mode').value='disable';
    ui.element('source-tls-mode').onchange();
    assert.equal(ui.run('sourcePayload().tls.cert'), undefined);
    ui.element('source-type').value='sqlite';
    ui.element('source-type').onchange();
    assert.equal(ui.element('source-tls-wrap').hidden, true);
    assert.equal(ui.run('sourcePayload().tls.mode'), 'default');
    ui.run('editSource()');
    assert.equal(ui.run('sourcePayload().tls.mode'), 'default');
    assert.equal(ui.element('source-tls-ca').value, '');
});

test('failed source save keeps TLS inputs and releases the save button', async () => {
    const ui=uiContext();
    ui.run(`editSource({id:'pg',type:'postgresql',tls:{mode:'verify-full',ca:'/certs/ca.pem'}});
        api=async()=>{throw new Error('network disconnected');};`);
    await ui.element('save-source').onclick();
    assert.equal(ui.element('source-tls-ca').value, '/certs/ca.pem');
    assert.equal(ui.element('source-tls-mode').value, 'verify-full');
    assert.equal(ui.element('save-source').disabled, false);
    assert.match(ui.element('source-message').textContent, /network disconnected/);
});

test('column editor displays saved types and recommends exact text only for new tasks', () => {
    const ui=uiContext();
    ui.run(`store.columns=[{name:'Id',sql_type:'bigint',feishu_type:1,nullable:false}];
        syncKeyOptions=()=>{};
        renderColumns();`);
    assert.match(ui.element('column-list').innerHTML, /value="1" selected/);
    ui.run(`renderColumns({columns:[{source:'Id',target:'编号',sql_type:'bigint'}]})`);
    assert.match(ui.element('column-list').innerHTML, /value="0" selected/);
    ui.run(`renderColumns({columns:[{source:'Id',target:'编号',sql_type:'bigint',target_type:2}]})`);
    assert.match(ui.element('column-list').innerHTML, /value="2" selected/);
    ui.run(`document.querySelector=(selector)=>selector.includes('data-column=')?{checked:true}:selector.includes('data-type-for=')?{value:'1'}:{value:'精确编号'};`);
    assert.equal(ui.run('selectedColumns()[0].target_type'), 1);
    assert.equal(ui.run('selectedColumns()[0].target'), '精确编号');
    assert.equal(ui.run('selectedColumns()[0].sql_type'), 'bigint');
});
