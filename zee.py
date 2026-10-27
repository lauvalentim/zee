#!/usr/bin/env python3
"""ZEE v0.3: consolidação Zeeschuimer e rede de interações do X.

Requisitos: Python >=3.10; pip install networkx
Execute sem parâmetros para o menu; --help para comandos.
Não implementa hashtags, ranking ou estatísticas gerais. Exporta GEXF temporal de eventos.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import networkx as nx

TWITTER_COLUMNS = (
    'collected_from_url', 'id', 'thread_id', 'timestamp', 'unix_timestamp',
    'link', 'body', 'author', 'author_fullname', 'author_id', 'author_avatar_url',
    'author_banner_url', 'author_followers', 'author_following', 'author_bio',
    'author_location', 'verified', 'source', 'language_guess',
    'possibly_sensitive', 'retweet_count', 'reply_count', 'like_count',
    'quote_count', 'impression_count', 'is_retweet', 'retweeted_user',
    'is_quote_tweet', 'quote_tweet_id', 'quote_author', 'quote_body',
    'quote_images', 'quote_videos', 'is_quote_withheld', 'is_reply',
    'replied_author', 'is_withheld', 'hashtags', 'urls', 'images', 'videos',
    'mentions', 'long_lat', 'place_name',
)
REQUIRED = {'id','timestamp','link','author','author_id','is_retweet',
            'retweeted_user','is_quote_tweet','quote_author','is_reply',
            'replied_author','mentions'}
EDGE_TYPES = ('retweet','reply','quote','mention')
STATUS_RE = re.compile(r'/status/(\d+)',re.I)
HANDLE_RE = re.compile(r'^@?([A-Za-z0-9_]{1,15})$')
SCI_RE = re.compile(r'^\d+[.,]\d+[eE][+]?\d+$')


def detectar_delimitador(path):
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        sample = f.readline()
    if not sample:
        raise ValueError(f'CSV vazio: {path}')
    options=[]
    for sep in (';',',','\t'):
        names=[x.strip() for x in next(csv.reader([sample],delimiter=sep),[])]
        options.append((sum(x in TWITTER_COLUMNS for x in names),len(names),sep))
    known,_,sep=max(options)
    if known < 2:
        raise ValueError(f'Cabeçalho do X não reconhecido: {path}')
    return sep


def csv_headers(path):
    sep=detectar_delimitador(path)
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        names=[x.strip() for x in next(csv.reader(f,delimiter=sep),[])]
    if len(names)!=len(set(names)):
        raise ValueError(f'Colunas duplicadas em {path}')
    return names


def reader(path):
    sep=detectar_delimitador(path)
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        parsed=csv.DictReader(f,delimiter=sep,restkey='__EXTRAS__',restval=None)
        names=[n.strip() for n in (parsed.fieldnames or [])]
        if not names or len(names)!=len(set(names)):
            raise ValueError(f'Cabeçalho ausente ou duplicado: {path}')
        for row in parsed:
            if row.get('__EXTRAS__') is not None or any(v is None for k,v in row.items() if k!='__EXTRAS__'):
                raise ValueError(f'Linha malformada: {path}, linha física próxima a {parsed.line_num}')
            yield {k.strip():v for k,v in row.items() if k!='__EXTRAS__'}


def post_id(row):
    m=STATUS_RE.search(row.get('link',''))
    return m.group(1) if m else row.get('id','')


def key_of(row):
    pid=post_id(row)
    if pid and not SCI_RE.match(pid):
        return ('post',pid)
    if row.get('link'):
        return ('link',row['link'])
    return None


def truthy(value):
    return str(value).strip().lower() in {'yes','true','sim','1','y'}


def handle(value):
    s=(value or '').strip()
    if s.startswith(('http://','https://')):
        parsed=urlparse(s)
        if parsed.hostname not in {'x.com','www.x.com','twitter.com','www.twitter.com','mobile.twitter.com'}:
            return ''
        s=parsed.path.strip('/').split('/')[0]
    m=HANDLE_RE.fullmatch(s)
    return m.group(1).lower() if m else ''


def mentions(value):
    if not value: return []
    s=value.strip()
    if s.startswith('['):
        try:
            parsed=json.loads(s)
            vals=([v.get('screen_name',v.get('username','')) if isinstance(v,dict) else v
                   for v in parsed] if isinstance(parsed,list) else [])
        except json.JSONDecodeError:
            vals=re.split(r'[,;\s]+',s)
    else:
        vals=re.split(r'[,;\s]+',s)
    return sorted({h for v in vals if (h:=handle(str(v)))})


def parse_timestamp(value):
    """Retorna datetime UTC apenas se o timestamp inclui fuso explícito."""
    s=(value or '').strip()
    if not s: return None,'ausente'
    try:
        d=datetime.fromisoformat(s.replace('Z','+00:00'))
        if d.tzinfo is None or d.utcoffset() is None:
            return None,'sem_fuso'
        return d.astimezone(timezone.utc),'ok'
    except ValueError:
        return None,'invalido'


def summarize_file(path):
    p=Path(path)
    digest=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            digest.update(chunk)
    return {'arquivo':str(p.resolve()), 'bytes':p.stat().st_size,
            'sha256':digest.hexdigest()}


class RunLogger:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.f=self.path.open('w',encoding='utf-8')
    def say(self,message,level='INFO'):
        line=f'[{datetime.now(timezone.utc).isoformat(timespec="seconds")}] [{level}] {message}'
        print(line)
        self.f.write(line+'\n'); self.f.flush()
    def close(self):
        self.f.close()


def unique_run_dir(base,kind):
    base=Path(base)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder=base/f'{kind}_{stamp}'
    folder.mkdir(parents=True,exist_ok=False)
    return folder


def save_report(folder, info, logger):
    dest=Path(folder)/'relatorio_processamento.txt'
    sections=[]
    for heading,values in info.items():
        sections += [heading.upper(),'-'*len(heading)]
        if isinstance(values,dict):
            sections.extend(f'{k}: {json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v}' for k,v in values.items())
        else:
            sections.append(str(values))
        sections.append('')
    dest.write_text('\n'.join(sections),encoding='utf-8')
    logger.say(f'Relatório salvo: {dest}')
    return dest


def merge(folder, output, recursive=False, log=None):
    """Une apenas o esquema original; contabiliza duplicatas por arquivo."""
    folder,output=Path(folder),Path(output)
    files=sorted(folder.rglob('*.csv') if recursive else folder.glob('*.csv'))
    files=[f for f in files if f.is_file() and f.resolve()!=output.resolve()]
    if not files: raise ValueError('Nenhum arquivo CSV encontrado.')
    expected=set(TWITTER_COLUMNS)
    for f in files:
        found=csv_headers(f)
        if set(found)!=expected or len(found)!=len(TWITTER_COLUMNS):
            raise ValueError(f'Esquema incompatível: {f.name}; faltantes={sorted(expected-set(found))}; extras={sorted(set(found)-expected)}')
    output.parent.mkdir(parents=True,exist_ok=True)
    tmp=output.with_name(output.name+'.tmp')
    seen=set(); per_file=[]; total=Counter(); duplicates_examples=[]; unidentified=0
    try:
        with tmp.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=TWITTER_COLUMNS,delimiter=';',lineterminator='\n')
            writer.writeheader()
            for f in files:
                c=Counter(); log.say(f'Lendo CSV: {f.name}; delimitador={repr(detectar_delimitador(f))}')
                for row in reader(f):
                    c['lidos']+=1; key=key_of(row)
                    if key is None:
                        unidentified+=1
                    elif key in seen:
                        c['duplicatas']+=1
                        if len(duplicates_examples)<10: duplicates_examples.append({'arquivo':f.name,'chave':str(key)})
                        continue
                    else:
                        seen.add(key)
                    writer.writerow(row); c['gravados']+=1
                total.update(c)
                per_file.append({'arquivo':f.name,**dict(c)})
                log.say(f'{f.name}: lidos={c["lidos"]}, duplicados={c["duplicatas"]}, gravados={c["gravados"]}')
        tmp.replace(output)
    finally:
        if tmp.exists(): tmp.unlink()
    return {'operacao':'merge','entrada':[summarize_file(f) for f in files],
            'saida':str(output),'linhas':dict(total),'por_arquivo':per_file,
            'duplicatas_amostra':duplicates_examples,
            'sem_chave_confiavel':unidentified,
            'aviso':'Linhas sem ID/link confiável são preservadas; duplicatas entre elas podem permanecer.'}



def export_dynamic_gexf(graph, path, node_times, edge_times):
    """GEXF 1.3 dinâmico: instantes OBSERVADOS, sem presumir atividade contínua.

    Cada spell registra um UTC instantâneo em que ocorreu uma interação.
    Não se escreve intervalo entre primeira/última observação, pois isso
    inventaria a continuidade de uma relação que pode ser episódica.
    """
    ns = 'http://gexf.net/1.3'
    ET.register_namespace('', ns)
    root = ET.Element(f'{{{ns}}}gexf', {'version': '1.3'})
    g = ET.SubElement(root, f'{{{ns}}}graph', {
        'mode': 'dynamic', 'timerepresentation': 'timestamp',
        'timeformat': 'dateTime', 'defaultedgetype': 'directed'})
    nodes_el = ET.SubElement(g, f'{{{ns}}}nodes')
    for n, attrs in graph.nodes(data=True):
        times=sorted(node_times.get(n, ()))
        if not times: continue
        el=ET.SubElement(nodes_el, f'{{{ns}}}node', {'id':str(n), 'label':str(attrs.get('label',n))})
        spells=ET.SubElement(el,f'{{{ns}}}spells')
        for t in times:
            ET.SubElement(spells,f'{{{ns}}}spell',{'timestamp':t})
    edges_el=ET.SubElement(g, f'{{{ns}}}edges')
    for i,(u,v,attrs) in enumerate(graph.edges(data=True)):
        times=sorted(edge_times.get((u,v), ()))
        if not times: continue
        el=ET.SubElement(edges_el,f'{{{ns}}}edge', {
            'id':str(i), 'source':str(u), 'target':str(v),
            'weight':str(attrs['weight'])})
        spells=ET.SubElement(el,f'{{{ns}}}spells')
        for t in times:
            ET.SubElement(spells,f'{{{ns}}}spell',{'timestamp':t})
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    # Verificação estrutural, independente da disponibilidade do Gephi.
    ET.parse(path)
    return {'nos_com_tempo':sum(bool(v) for v in node_times.values()),
            'arestas_com_tempo':sum(bool(v) for v in edge_times.values()),
            'instantes_de_arestas_distintos':sum(len(v) for v in edge_times.values())}

def process(source,output,include_self_loops=False,log=None):
    """Cria rede dirigida; nós usam @handle como chave por compatibilidade.

    IDs author_id são auditados nos autores conhecidos. Para destinos sem ID,
    NÃO se assume que um handle seja identidade persistente.
    """
    source,output=Path(source),Path(output)
    missing=REQUIRED-set(csv_headers(source))
    if missing: raise ValueError(f'Colunas obrigatórias ausentes: {sorted(missing)}')
    graph=nx.DiGraph(platform='Twitter/X',network_type='user_interactions',
                     direction_semantics='author_to_target',weight_semantics='interaction_event_count')
    c=Counter(); kinds=Counter(); seen=set(); identity=defaultdict(set); invalid_examples=[]
    node_times=defaultdict(set); edge_times=defaultdict(set)
    time_stats=Counter(); duplicate_examples=[]
    for row in reader(source):
        c['publicacoes_lidas']+=1
        key=key_of(row)
        if key is not None:
            if key in seen:
                c['duplicatas_ignoradas']+=1
                if len(duplicate_examples)<10: duplicate_examples.append(str(key))
                continue
            seen.add(key)
        else:
            c['publicacoes_sem_chave']+=1
        actor=handle(row.get('author',''))
        if not actor:
            c['sem_autor_valido']+=1; continue
        uid=(row.get('author_id') or '').strip()
        if uid and not SCI_RE.match(uid): identity[actor].add(uid)
        elif uid: c['ids_autor_notacao_cientifica']+=1
        if actor not in graph:
            graph.add_node(actor,label='@'+actor,username=actor,node_type='user',identity_status='handle')
        node=graph.nodes[actor]
        for col,attr in [('author_fullname','fullname'),('author_followers','followers'),
                         ('author_following','following'),('verified','verified')]:
            if row.get(col): node[attr]=str(row[col])
        if uid and not SCI_RE.match(uid): node['author_id']=uid
        events=[]
        if truthy(row.get('is_retweet','')): events.append(('retweet',row.get('retweeted_user','')))
        if truthy(row.get('is_reply','')): events.append(('reply',row.get('replied_author','')))
        if truthy(row.get('is_quote_tweet','')): events.append(('quote',row.get('quote_author','')))
        events.extend(('mention',h) for h in mentions(row.get('mentions','')))
        date,state=parse_timestamp(row.get('timestamp',''))
        time_stats[state]+=1
        stamp=date.isoformat(timespec='microseconds').replace('+00:00','Z') if date else ''
        if stamp: node_times[actor].add(stamp)
        for kind,raw in events:
            target=handle(raw)
            if not target:
                c['alvos_invalidos']+=1
                if len(invalid_examples)<10: invalid_examples.append({'tipo':kind,'valor':str(raw)[:40]})
                continue
            if target==actor and not include_self_loops:
                c['autolacos_descartados']+=1; continue
            if target not in graph:
                graph.add_node(target,label='@'+target,username=target,node_type='user',identity_status='handle')
            if not graph.has_edge(actor,target):
                graph.add_edge(actor,target,weight=0,retweet_interactions=0,reply_interactions=0,
                               quote_interactions=0,mention_interactions=0,first_timestamp='',last_timestamp='')
            edge=graph[actor][target]; edge['weight']+=1; edge[f'{kind}_interactions']+=1
            if stamp:
                node_times[actor].add(stamp)
                node_times[target].add(stamp)
                edge_times[(actor,target)].add(stamp)
                if not edge['first_timestamp'] or stamp<edge['first_timestamp']:
                    edge['first_timestamp']=stamp
                if not edge['last_timestamp'] or stamp>edge['last_timestamp']:
                    edge['last_timestamp']=stamp
                # Inteiros Unix (segundos desde 1970-01-01 UTC) para filtros numéricos.
                epoch=int(date.timestamp())
                edge['first_timestamp_unix']=min(epoch,edge.get('first_timestamp_unix',epoch))
                edge['last_timestamp_unix']=max(epoch,edge.get('last_timestamp_unix',epoch))
            else:
                c['interacoes_sem_data_valida']+=1
            c['interacoes_observadas']+=1; kinds[kind]+=1
    ambiguous={h:sorted(ids) for h,ids in identity.items() if len(ids)>1}
    for h,ids in identity.items():
        if h in graph:
            graph.nodes[h]['identity_status']='conflicting_author_ids' if len(ids)>1 else 'author_id_observed'
            if len(ids)>1: graph.nodes[h]['author_id']=''
    output.parent.mkdir(parents=True,exist_ok=True)
    gexf=output.with_suffix('.gexf'); json_path=output.with_suffix('.json')
    temporal_path=output.with_name(output.name+'_temporal.gexf')
    nx.write_gexf(graph,gexf,encoding='utf-8',prettyprint=True)
    if edge_times:
        temporal_stats=export_dynamic_gexf(graph,temporal_path,node_times,edge_times)
        log.say(f'GEXF dinâmico criado: {temporal_path.name}; '+
                f'{temporal_stats["arestas_com_tempo"]} relações com eventos datados.')
    else:
        temporal_stats=None
        log.say('Nenhuma interação datada: GEXF dinâmico não foi criado.','WARN')
    result={'metadata':{'platform':'Twitter/X','graph_type':'directed_user_interaction',
            'direction':'author -> target','edge_weight':'observed_interaction_events',
            'source_csv':source.name, 'counters':dict(c),
            'interactions_by_type':dict(kinds),'self_loops_included':include_self_loops,
            'timeline_dynamic':bool(temporal_stats)},
            'nodes':[{'id':str(n),**a} for n,a in graph.nodes(data=True)],
            'links':[{'source':str(u),'target':str(v),**a} for u,v,a in graph.edges(data=True)]}
    json_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    log.say(f'Grafo exportado: {graph.number_of_nodes()} nós, {graph.number_of_edges()} arestas, {c["interacoes_observadas"]} eventos.')
    if ambiguous: log.say(f'ATENÇÃO: {len(ambiguous)} handles possuem mais de um author_id observado.','WARN')
    if time_stats['invalido'] or time_stats['sem_fuso']:
        log.say(f'Datas não usadas nos limites temporais: {dict(time_stats)}','WARN')
    return {'operacao':'process','entrada':summarize_file(source),'saida':{'gexf':str(gexf),'json':str(json_path),
                 'gexf_temporal':str(temporal_path) if temporal_stats else None},
        'volume':dict(c),'interacoes_por_tipo':dict(kinds),
        'rede':{'nos':graph.number_of_nodes(),'arestas':graph.number_of_edges(),
                'direcionada':True,'autolacos':include_self_loops},
        'datas':{'contagens':dict(time_stats), 'rede_dinamica':temporal_stats,
                 'interacoes_sem_data_valida':c['interacoes_sem_data_valida'],
                 'regra':'ISO 8601 com fuso explícito, UTC; atributos Unix são integer em segundos. GEXF dinâmico 1.3 usa timestamps ISO dateTime como spells discretos. Publicações sem data não entram no GEXF temporal.'},
        'identidade':{'handles_com_multiplos_ids':ambiguous,
                     'autores_com_id_observado':len(identity),
                     'regra':'Nós continuam indexados por handle; author_id de origem é atributo e não identifica destinos.'},
        'amostras_de_auditoria':{'duplicatas':duplicate_examples,'alvos_invalidos':invalid_examples},
        'limites':{'timeline':'A rede estática conserva todas as interações. A rede dinâmica contém somente nós/arestas com instantes válidos de ocorrência; peso da aresta nela é agregado para o período total, não é um peso que varia com o tempo.',
                   'duplicatas':'A primeira ocorrência prevalece; linhas sem chave confiável podem se repetir.'}}


def execute(kind,inp,out_base,recursive=False,self_loops=False):
    folder=unique_run_dir(out_base,kind)
    log=RunLogger(folder/'execucao.log.txt')
    report={'execucao':{'operacao':kind,'inicio_utc':datetime.now(timezone.utc).isoformat(),
                        'pasta':str(folder),'versao':'0.3','status':'EM_ANDAMENTO'}}
    try:
        log.say(f'INÍCIO: {kind}; origem={inp}; destino={folder}')
        if kind=='merge':
            report['resultado']=merge(inp,folder/'twitter_consolidado.csv',recursive,log)
        elif kind=='process':
            report['resultado']=process(inp,folder/(Path(inp).stem+'_rede'),self_loops,log)
        report['execucao']['status']='CONCLUÍDO'
        log.say('Execução concluída com sucesso.')
    except Exception as exc:
        report['execucao']['status']='FALHA'
        report['erro']={'tipo':type(exc).__name__,'mensagem':str(exc)}
        log.say(f'FALHA: {type(exc).__name__}: {exc}','ERROR')
        log.say(traceback.format_exc(),'ERROR')
        raise
    finally:
        report['execucao']['fim_utc']=datetime.now(timezone.utc).isoformat()
        save_report(folder,report,log)
        (folder/'configuracao.json').write_text(json.dumps({
            'operacao':kind,'entrada':str(inp),'recursive':recursive,
            'self_loops':self_loops,'pasta_saida':str(folder)
            },indent=2,ensure_ascii=False),encoding='utf-8')
        log.close()
    return folder


def listar_csvs(folder):
    return sorted((p for p in Path(folder).glob('*.csv') if p.is_file()),key=lambda p:p.name.casefold())


def escolher_csv(paths):
    if not paths:
        print('Nenhum CSV disponível.'); return None
    for i,f in enumerate(paths,1): print(f'  {i}. {f}')
    while True:
        ans=input('Número do CSV (0 = voltar): ').strip()
        if ans=='0': return None
        if ans.isdigit() and 1<=int(ans)<=len(paths): return paths[int(ans)-1]
        print('Escolha inválida.')


def menu_interativo():
    folder=Path(__file__).resolve().parent
    output=folder/'output'
    while True:
        files=listar_csvs(folder)
        print('\nZEE v0.3 — Zeeschuimer / Twitter-X')
        print('1. Consolidar CSVs (merge)')
        print('2. Gerar rede usuário → usuário (process)')
        print('3. Listar CSVs')
        print('0. Sair')
        try:
            choice=input('Escolha: ').strip()
            if choice=='0': return
            if choice=='1':
                if len(files)<2:
                    print('São necessários pelo menos dois CSVs na pasta do script.'); continue
                result=execute('merge',folder,output)
                print(f'Saída: {result}')
            elif choice=='2':
                # Consolidados anteriores são salvos em subpastas de execução.
                generated=sorted(output.glob('merge_*/twitter_consolidado.csv')) if output.exists() else []
                selected=escolher_csv(files+generated)
                if selected:
                    result=execute('process',selected,output)
                    print(f'Saída: {result}')
            elif choice=='3':
                for p in files: print(p.name)
            else: print('Opção inválida.')
        except (EOFError,KeyboardInterrupt):
            print('\nInterrompido.'); return
        except Exception as exc:
            print(f'Erro: {exc}. Veja execucao.log.txt e relatorio_processamento.txt.',file=sys.stderr)


def main():
    if len(sys.argv)==1: return menu_interativo()
    p=argparse.ArgumentParser(description='ZEE v0.3 — CSV Zeeschuimer e Gephi')
    sub=p.add_subparsers(dest='command',required=True)
    m=sub.add_parser('merge'); m.add_argument('folder'); m.add_argument('-o','--output',required=True,
        help='Pasta-base de resultados (cada execução cria subpasta)'); m.add_argument('--recursive',action='store_true')
    q=sub.add_parser('process'); q.add_argument('csv'); q.add_argument('-o','--output',required=True,
        help='Pasta-base de resultados (cada execução cria subpasta)'); q.add_argument('--self-loops',action='store_true')
    args=p.parse_args()
    try:
        folder=execute(args.command,args.folder if args.command=='merge' else args.csv,
                       args.output,recursive=getattr(args,'recursive',False),
                       self_loops=getattr(args,'self_loops',False))
        print(f'Resultados em: {folder}')
    except Exception as exc:
        p.exit(1,f'Erro: {exc}\n')


if __name__=='__main__':
    main()
