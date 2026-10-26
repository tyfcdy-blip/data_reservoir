import { esc, icon, field, select } from './core.js';

const blankGroup = () => ({conditions:[{field:'',valuesText:''}]});
const stringDimensions = dataset => dataset.definition.fields.filter(column=>column.type==='string' && column.dimension);
const columnLabel = column => column.description ? column.description+' · '+column.name : column.name;

export function scopeDescription(scope, dataset) {
  if(scope.mode==='all')return '全部数据（无行范围限制）';
  const fields=dataset?.definition.fields || [];
  return scope.groups.map((group,index)=>'范围 '+(index+1)+'：'+group.conditions.map(condition=>{
    const column=fields.find(item=>item.name===condition.field);
    return (column?.description || condition.field)+' 属于 '+condition.values.map(value=>'「'+value+'」').join('、');
  }).join(' 且 ')).join('；或者 ');
}

// 输入只按换行拆分；不得修剪或去重，否则会静默改变管理员填写的精确授权值。
export function readScopes(scopes, datasets) {
  const rowScopes={};let valueCount=0;
  for(const dataset of datasets){
    const scope=scopes.get(dataset.slug);
    if(!scope)throw new Error('请配置 '+dataset.name+' 的数据范围。');
    if(scope.mode==='all'){rowScopes[dataset.slug]={mode:'all',groups:[]};continue;}
    if(!scope.groups.length || scope.groups.length>20)throw new Error(dataset.name+' 需要 1—20 个范围组。');
    const columns=stringDimensions(dataset);
    const groups=scope.groups.map((group,index)=>{
      if(!group.conditions.length || group.conditions.length>5)throw new Error('每个范围组需要 1—5 个条件。');
      const used=new Set();
      return {conditions:group.conditions.map(condition=>{
        const column=columns.find(item=>item.name===condition.field);
        if(!column)throw new Error(dataset.name+' 的范围 '+(index+1)+' 请选择字符串维度字段。');
        if(used.has(column.name))throw new Error('同一范围组不能重复选择 '+columnLabel(column)+'。');
        used.add(column.name);
        const values=condition.valuesText.split('\n');
        if(values.length>100)throw new Error('每个条件最多填写 100 个允许值。');
        if(values.some(value=>!value.trim()))throw new Error(columnLabel(column)+' 请逐行填写允许值，不要留空行。');
        if(values.some(value=>/[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u.test(value)))throw new Error('允许值不能包含制表符等控制字符。');
        if(values.some(value=>[...value].length>(column.max_length || 255)))throw new Error(columnLabel(column)+' 每个值最多 '+(column.max_length || 255)+' 个字符。');
        if(new Set(values).size!==values.length)throw new Error(columnLabel(column)+' 的允许值不能重复。');
        valueCount+=values.length;
        return {field:column.name,values};
      })};
    });
    rowScopes[dataset.slug]={mode:'filtered',groups};
  }
  if(valueCount>2000)throw new Error('所有数据表的允许值合计不能超过 2000 个。');
  return rowScopes;
}

function conditionHtml(condition, index, columns) {
  return '<div class="key-scope-condition" data-condition="'+index+'">'+
    field('字段',select('scope_field',[['','请选择字段'],...columns.map(column=>[column.name,columnLabel(column)])],condition.field))+
    field('允许值（每行一个）','<textarea name="scope_values" rows="2" placeholder="如：A店铺">'+esc(condition.valuesText)+'</textarea>')+
    '<button type="button" class="btn icon-only danger" data-remove-condition aria-label="删除条件">'+icon('close')+'</button></div>';
}
function scopeCard(dataset, scope) {
  const columns=stringDimensions(dataset),groups=scope.groups;
  return '<section class="key-scope-card" data-scope="'+esc(dataset.slug)+'"><div class="key-scope-heading"><div><h4>'+esc(dataset.name)+'</h4><code>'+esc(dataset.slug)+'</code></div>'+
    field('数据范围',select('scope_mode',[['filtered','限定范围'],['all','全部数据']],scope.mode))+'</div>'+
    (scope.mode==='all'?'<div class="notice warning">允许访问此表的全部记录，不限制渠道、店铺或负责人。仅向需要完整范围的调用方授予此权限。</div>':
      (columns.length?'<div class="key-scope-groups">'+groups.map((group,index)=>
        '<div class="key-scope-group" data-group="'+index+'"><div class="key-group-heading"><strong>范围 '+(index+1)+'</strong><span class="subtle">以下条件同时满足（且）</span>'+
        '<button type="button" class="btn link small danger" data-remove-group>删除此范围</button></div>'+
        group.conditions.map((condition,position)=>conditionHtml(condition,position,columns)).join('')+
        '<button type="button" class="btn small" data-add-condition'+(group.conditions.length>=5?' disabled':'')+'>'+icon('plus')+'添加条件</button></div>').join('<div class="key-scope-or">或者满足</div>')+
        '</div><button type="button" class="btn small" data-add-group'+(groups.length>=20?' disabled':'')+'>'+icon('plus')+'添加另一组范围（或）</button>':
        '<div class="notice warning">此表没有可用的字符串维度，无法设置限定范围。请先在数据表配置中确认维度，或明确选择全部数据。</div>'))+'</section>';
}

export function createScopeEditor(container, onChange) {
  const scopes=new Map();let datasets=[];
  const render=()=>{
    container.innerHTML=datasets.length?datasets.map(dataset=>scopeCard(dataset,scopes.get(dataset.slug))).join(''):'<p class="subtle">选择接口或数据表后，在这里设置允许访问的记录。</p>';
  };
  const locate=element=>{
    const card=element.closest('[data-scope]'),group=element.closest('[data-group]'),condition=element.closest('[data-condition]');
    return {scope:card?scopes.get(card.dataset.scope):null,groupIndex:group?Number(group.dataset.group):null,conditionIndex:condition?Number(condition.dataset.condition):null};
  };
  container.addEventListener('input',event=>{
    if(event.target.name!=='scope_values')return;
    const {scope,groupIndex,conditionIndex}=locate(event.target);
    scope.groups[groupIndex].conditions[conditionIndex].valuesText=event.target.value;onChange();
  });
  container.addEventListener('change',event=>{
    const {scope,groupIndex,conditionIndex}=locate(event.target);if(!scope)return;
    if(event.target.name==='scope_mode'){
      scope.mode=event.target.value;scope.groups=scope.mode==='filtered'?[blankGroup()]:[];render();onChange();
    }else if(event.target.name==='scope_field'){
      scope.groups[groupIndex].conditions[conditionIndex].field=event.target.value;onChange();
    }
  });
  container.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button || button.disabled)return;
    const {scope,groupIndex,conditionIndex}=locate(button);if(!scope)return;
    if(button.hasAttribute('data-add-group') && scope.groups.length<20)scope.groups.push(blankGroup());
    else if(button.hasAttribute('data-remove-group'))scope.groups.splice(groupIndex,1);
    else if(button.hasAttribute('data-add-condition') && scope.groups[groupIndex].conditions.length<5)scope.groups[groupIndex].conditions.push({field:'',valuesText:''});
    else if(button.hasAttribute('data-remove-condition'))scope.groups[groupIndex].conditions.splice(conditionIndex,1);
    else return;
    render();onChange();
  });
  return {
    setDatasets(selected, reset=false){
      datasets=selected;if(reset)scopes.clear();
      const allowed=new Set(selected.map(dataset=>dataset.slug));
      for(const slug of scopes.keys())if(!allowed.has(slug))scopes.delete(slug);
      selected.forEach(dataset=>{if(!scopes.has(dataset.slug))scopes.set(dataset.slug,{mode:'filtered',groups:[blankGroup()]});});
      render();
    },
    read:()=>readScopes(scopes,datasets),
  };
}

