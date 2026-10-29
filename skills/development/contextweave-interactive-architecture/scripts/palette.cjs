// Structured color decisions only. No guessing colors from request keywords.
const COLOR_NAMES={"红":"#C00000","正红":"#C00000","蓝":"#1F6FB4","科技蓝":"#1F6FB4","绿":"#2E7D32","橙":"#E65100","暖橙":"#E65100","紫":"#6A1B9A","金":"#B8860B","黑":"#000000","白":"#FFFFFF",red:"#C00000",blue:"#1F6FB4",green:"#2E7D32",orange:"#E65100",purple:"#6A1B9A",gold:"#B8860B",black:"#000000",white:"#FFFFFF"};
const PRESETS=['corporate_red','corporate_blue','tech_blue','red-gold-compact-v1','blue-compact-v1'];
const failure=(code,message)=>({status:'error',error:{code,message,recoverable:true}});
function validateBasePalette(raw){
 let value;try{value=typeof raw==='string'?JSON.parse(raw):raw;}catch(e){return {error:failure('INVALID_BASE_PALETTE','base_palette 必须是合法 JSON')};}
 const fail=m=>({error:failure('INVALID_BASE_PALETTE',m)});
 if(!value||typeof value!=='object'||Array.isArray(value)||!Object.keys(value).length)return fail('base_palette 必须是非空对象');
 const rich=Object.hasOwn(value,'version');const allowed=rich?['version','primary','secondary','background']:['primary','style_preset'];
 if(Object.keys(value).some(k=>!allowed.includes(k)))return fail('base_palette 包含未知字段或冲突字段；第二色／背景需要 version:1');
 if(rich&&value.version!==1)return fail('不支持的 base_palette.version');
 const result=rich?{version:1}:{};
 for(const key of ['primary','secondary','background'])if(value[key]!=null){
  if(typeof value[key]!=='string')return fail(`${key} 必须是颜色字符串`);
  const color=value[key].trim();const named=Object.hasOwn(COLOR_NAMES,color.toLowerCase())?COLOR_NAMES[color.toLowerCase()]:null;
  if(/^#[0-9a-fA-F]{6}$/.test(color))result[key]=color.toUpperCase();else if(named)result[key]=named;else return fail(`${key} 需要 6 位 Hex 或受支持色名`);
 }
 if(rich&&!result.primary)return fail('version:1 必须提供 primary');
 if(value.style_preset!=null){if(!PRESETS.includes(value.style_preset))return fail('不支持的 style_preset');result.style_preset=value.style_preset;}
 if(result.primary&&result.style_preset?.endsWith('-v1'))return fail('版本化视觉预设拥有其颜色，请省略 primary');
 if(!Object.keys(result).length)return fail('需要 primary 或 style_preset');
 return {value:result};
}
function choosePalette(palette,useDefault,existing){
 if(typeof useDefault!=='boolean')return {error:failure('PALETTE_CONFLICT','useDefaultPalette 必须为布尔值')};
 if(useDefault&&(palette||existing))return {error:failure('PALETTE_CONFLICT','默认选择不能与选定色板或已有图的继承混用')};
 if(palette)return {choice:'selected'};
 if(existing)return {choice:'inherit'};
 if(useDefault)return {choice:'default'};
 return {error:failure('PALETTE_SELECTION_REQUIRED','先将配色选择传入 base_palette；用户未指定配色时显式使用 --use_default_palette，已有图沿用 session_id 配色')};
}
function validatePaletteCapability(caps,palette,route){
 if(!palette?.version)return null;
 const supported=caps?.co_design?.palette;
 if(route!=='co_design'||!supported?.versions?.includes(palette.version)||!supported.routes?.includes(route)||Object.keys(palette).some(k=>!supported.fields?.includes(k)))return failure('PALETTE_UNSUPPORTED','此入口或后端未声明所用配色协议能力；不能丢弃第二色／背景或改用默认色板');
 return null;
}
module.exports={validateBasePalette,choosePalette,validatePaletteCapability,COLOR_NAME_MAP:COLOR_NAMES,STYLE_PRESET_ENUM:PRESETS};
