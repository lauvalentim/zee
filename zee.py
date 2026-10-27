#!/usr/bin/env python3
"""ZEE v0.8: consolidação Zeeschuimer e rede de interações do X.

Requisitos: Python >=3.10; pip install networkx plotly
Execute sem parâmetros para o menu; --help para comandos.
Inclui metadados (soma, ranking de posts e hashtags). Inclui dashboard HTML de volume diário de postagens.
"""
import argparse
import math
import csv
import shutil
import os
import tempfile
import hashlib
import json
import re
import sys
import traceback
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse
import networkx as nx
# Plotly é carregado somente quando a opção Timeline é executada.

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
        options.append((sum(x in TWITTER_COLUMNS or x in {'repost_count','hashtag','soma_count'} for x in names),len(names),sep))
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


def parse_timestamp(value, unix_value=''):
    """Obtém uma data UTC confiável de timestamp ou unix_timestamp.

    Prioridades: ISO com fuso -> unix_timestamp numérico -> aviso.
    NÃO presume um fuso para horários locais sem indicação.
    Números Unix são segundos, com suporte a milissegundos inteiros.
    """
    s=(value or '').strip()
    reason='ausente' if not s else 'invalido'
    if s:
        try:
            date=datetime.fromisoformat(s.replace('Z','+00:00'))
            if date.tzinfo is not None and date.utcoffset() is not None:
                return date.astimezone(timezone.utc),'timestamp_iso'
            reason='sem_fuso'
        except ValueError:
            # Alguns exportadores usam formatos não ISO. Não adivinhar o fuso.
            reason='invalido'
    u=(unix_value or '').strip()
    if u:
        try:
            value_decimal=Decimal(u)
            if not value_decimal.is_finite():
                raise ValueError('número Unix não finito')
            # 13 dígitos inteiros significam normalmente milissegundos Unix.
            # Não converter IDs de postagem nem números malformados.
            if value_decimal == value_decimal.to_integral_value() and abs(value_decimal)>=Decimal('100000000000'):
                value_decimal /= Decimal(1000)
            date=datetime.fromtimestamp(float(value_decimal), tz=timezone.utc)
            return date,'unix_timestamp'
        except (InvalidOperation, ValueError, OverflowError, OSError):
            return None,reason+'_unix_invalido'
    return None,reason


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



def export_event_gexf(graph, events, path):
    """Cada edge GEXF 1.3 equivale a UM evento datado, com weight=1.

    O elemento timestamp (não um attvalue textual) define o instante do evento.
    Atributos adicionais preservam tipo e publicação. Mesmo timestamp repetido
    em eventos distintos NÃO os funde. Nós recebem spells por instante observado.
    """
    ns='http://gexf.net/1.3'
    ET.register_namespace('',ns)
    tag=lambda name: f'{{{ns}}}{name}'
    root=ET.Element(tag('gexf'), {'version':'1.3'})
    g=ET.SubElement(root,tag('graph'), {'mode':'dynamic','timerepresentation':'timestamp',
         'timeformat':'dateTime','defaultedgetype':'directed'})
    attrs=ET.SubElement(g,tag('attributes'),{'class':'edge','mode':'static'})
    for key,title,typ in [('0','interaction_type','string'),('1','post_id','string'),
                          ('2','timestamp_iso','string'),('3','unix_timestamp','long')]:
        ET.SubElement(attrs,tag('attribute'),{'id':key,'title':title,'type':typ})
    nodes=ET.SubElement(g,tag('nodes'))
    node_times=defaultdict(set)
    for e in events:
        node_times[e['source']].add(e['timestamp'])
        node_times[e['target']].add(e['timestamp'])
    for n in sorted(node_times):
        el=ET.SubElement(nodes,tag('node'),{'id':n,'label':'@'+n})
        spells=ET.SubElement(el,tag('spells'))
        for stamp in sorted(node_times[n]):
            ET.SubElement(spells,tag('spell'),{'timestamp':stamp})
    edges=ET.SubElement(g,tag('edges'))
    for i,e in enumerate(events):
        el=ET.SubElement(edges,tag('edge'),{'id':str(i),'source':e['source'],
              'target':e['target'],'kind':f'event_{i}', 'weight':'1', 'timestamp':e['timestamp']})
        values=ET.SubElement(el,tag('attvalues'))
        for key,value in [('0',e['type']),('1',e['post_id']),('2',e['timestamp']),
                          ('3',str(e['unix_timestamp']))]:
            ET.SubElement(values,tag('attvalue'),{'for':key,'value':value})
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    ET.parse(path)
    return {'nos_datados':len(node_times),'arestas_eventos_datados':len(events),
            'instantes_distintos':len({e['timestamp'] for e in events})}


def process(source,output,include_self_loops=False,log=None,mode='static'):
    """Rede de interações; estática agrega pares e temporal preserva eventos.

    Regra de peso da estática: eventos (inclui reply + mention do mesmo post).
    Rede temporal: cada aresta representa um evento específico do CSV e tem
    timestamp da publicação. Publicações sem data válida NÃO entram na dinâmica.
    """
    if mode not in ('static','temporal'):
        raise ValueError(f'Modo de rede desconhecido: {mode}')
    source,output=Path(source),Path(output)
    missing=REQUIRED-set(csv_headers(source))
    if missing: raise ValueError(f'Colunas obrigatórias ausentes: {sorted(missing)}')
    graph=nx.DiGraph(platform='Twitter/X',network_type='explicit_mentions',
                     direction_semantics='author_to_target',weight_semantics='number_of_events')
    counts=Counter(); kinds=Counter(); seen=set(); identity=defaultdict(set)
    time_stats=Counter(); bad_dates=[]; duplicates=[]; invalid_targets=[]
    dated_events=[]; all_events=[]
    for row in reader(source):
        counts['publicacoes_lidas']+=1
        key=key_of(row)
        if key is not None:
            if key in seen:
                counts['duplicatas_ignoradas']+=1
                if len(duplicates)<10: duplicates.append(str(key))
                continue
            seen.add(key)
        else:
            counts['publicacoes_sem_chave']+=1
        actor=handle(row.get('author',''))
        if not actor:
            counts['sem_autor_valido']+=1
            continue
        uid=(row.get('author_id') or '').strip()
        if uid and not SCI_RE.match(uid): identity[actor].add(uid)
        elif uid: counts['author_ids_notacao_cientifica']+=1
        if actor not in graph:
            graph.add_node(actor,label='@'+actor,username=actor,node_type='user')
        node=graph.nodes[actor]
        for col,attr in [('author_fullname','fullname'),('author_followers','followers'),
                         ('author_following','following'),('verified','verified')]:
            if row.get(col): node[attr]=str(row[col])
        if uid and not SCI_RE.match(uid): node['author_id']=uid
        # Esta modalidade usa SOMENTE a coluna mentions. Respostas, citações
        # e repostagens NÃO são reinterpretados como menções implícitas.
        candidates=[('mention',h) for h in mentions(row.get('mentions',''))]
        date,source_of_time=parse_timestamp(row.get('timestamp',''),row.get('unix_timestamp',''))
        time_stats[source_of_time]+=1
        if date is None and len(bad_dates)<10:
            bad_dates.append({'post_id':post_id(row),'timestamp':row.get('timestamp',''),
                              'unix_timestamp':row.get('unix_timestamp',''),'motivo':source_of_time})
        stamp=date.isoformat(timespec='microseconds').replace('+00:00','Z') if date else ''
        epoch=int(date.timestamp()) if date else None
        for kind,raw in candidates:
            target=handle(raw)
            if not target:
                counts['alvos_invalidos']+=1
                if len(invalid_targets)<10: invalid_targets.append({'tipo':kind,'valor':str(raw)[:80]})
                continue
            if target==actor and not include_self_loops:
                counts['autolacos_descartados']+=1
                continue
            if target not in graph:
                graph.add_node(target,label='@'+target,username=target,node_type='user')
            if not graph.has_edge(actor,target):
                graph.add_edge(actor,target,weight=0,retweet_interactions=0,
                       reply_interactions=0,quote_interactions=0,mention_interactions=0)
            edge=graph[actor][target]
            edge['weight']+=1
            edge[f'{kind}_interactions']+=1
            counts['eventos_observados']+=1
            kinds[kind]+=1
            event={'source':actor,'target':target,'type':kind,'post_id':post_id(row),
                   'timestamp':stamp,'unix_timestamp':epoch,'date_origin':source_of_time}
            all_events.append(event)
            if date:
                dated_events.append(event)
                counts['eventos_com_data_valida']+=1
            else:
                counts['eventos_sem_data_valida']+=1
    ambiguous={h:sorted(ids) for h,ids in identity.items() if len(ids)>1}
    for h,ids in identity.items():
        if h in graph:
            graph.nodes[h]['identity_status']='conflicting_author_ids' if len(ids)>1 else 'author_id_observed'
            if len(ids)>1: graph.nodes[h]['author_id']=''
    output.parent.mkdir(parents=True,exist_ok=True)
    paths={}
    if mode=='static':
        dest=output.with_suffix('.gexf')
        nx.write_gexf(graph,dest,encoding='utf-8',prettyprint=True)
        paths['gexf_estatico']=str(dest)
        log.say('Rede estática agregada salva: '+str(dest))
    else:
        if not dated_events:
            raise ValueError('Nenhum evento com data válida. Não é possível gerar uma rede temporal.')
        dest=output.with_name(output.name+'_temporal.gexf')
        stats=export_event_gexf(graph,dated_events,dest)
        paths['gexf_temporal']=str(dest)
        log.say('Rede temporal por evento salva: '+str(dest))
        log.say('Eventos com data: '+str(len(dated_events))+'; instantes: '+str(stats['instantes_distintos']))
    events_csv=output.with_name(output.name+'_eventos.csv')
    with events_csv.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['source','target','type','post_id',
               'timestamp','unix_timestamp','date_origin'],delimiter=';')
        writer.writeheader(); writer.writerows(all_events)
    paths['csv_eventos']=str(events_csv)
    data={'metadata':{'platform':'Twitter/X','mode':mode,'source_csv':source.name,
                      'counts':dict(counts),'interactions_by_type':dict(kinds)},
          'nodes':[{'id':str(n),**a} for n,a in graph.nodes(data=True)],
          'links':[{'source':str(u),'target':str(v),**a} for u,v,a in graph.edges(data=True)]}
    json_dest=output.with_suffix('.json')
    json_dest.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    paths['json_rede_agregada']=str(json_dest)
    log.say(f'Rede: {graph.number_of_nodes()} nós, {graph.number_of_edges()} pares, '
            f'{counts["eventos_observados"]} eventos; {counts["eventos_sem_data_valida"]} sem data.')
    if ambiguous: log.say(f'{len(ambiguous)} perfis com IDs contraditórios.','WARN')
    if bad_dates: log.say('Ocorreram datas não interpretáveis; consultar relatório.','WARN')
    return {'operacao':'process','modo':mode,'entrada':summarize_file(source),
       'saidas':paths,'contagens':dict(counts),'interacoes_por_tipo':dict(kinds),
       'rede_agregada_referencia':{'nos':graph.number_of_nodes(),
              'pares_direcionados':graph.number_of_edges()},
       'datas':{'origens':dict(time_stats),'datas_invalidas_amostra':bad_dates,
           'regra':'Data do post por evento: ISO com fuso; fallback unix_timestamp; UTC. '
                    'Eventos sem data ficam no CSV de eventos, mas não na rede dinâmica. '
                    'Nenhuma primeira/última ocorrência é utilizada como tempo do evento.'},
       'auditoria':{'duplicatas_amostra':duplicates,'alvos_invalidos_amostra':invalid_targets,
          'conflitos_handle_author_id':ambiguous},
       'limites':{'escopo':'Arestas exclusivamente de menções explícitas na coluna mentions; respostas, citações e repostagens não são contadas.',
          'identidade':'Destino identificado por handle, não por ID persistente.',
          'evento':'Uma mesma publicação pode produzir eventos de tipos diferentes.',
          'temporal':'Cada aresta do GEXF temporal representa um evento de peso 1; '
                     'a Timeline pode exibir instantes ou seleção acumulada conforme sua janela.'}}

#  METADADOS: preservam a ordem e os valores das outras colunas.
#  Datas, hashtags e contagens são atributos de publicações, não da rede.

def parse_count(raw, column, rownum):
    """Aceita inteiros não negativos. Vazio equivale a zero e é auditado."""
    value = str(raw or '').strip()
    if not value:
        return 0, True
    # Não converte valores fracionários nem arredonda números inválidos.
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError(f'Registro {rownum}: {column}={value!r} não é inteiro não negativo.')
    return int(value), False


def metadata_sum(source, folder, log):
    """Inclui/atualiza soma_count NO CSV selecionado, com backup e troca atômica."""
    source = Path(source)
    headers = csv_headers(source)
    repost_column = 'repost_count' if 'repost_count' in headers else 'retweet_count'
    required = {'reply_count', 'like_count', repost_column}
    if not required.issubset(headers):
        raise ValueError(f'Colunas ausentes para soma: {sorted(required - set(headers))}.')
    sep = detectar_delimitador(source)
    input_stats = summarize_file(source)
    log.say('Iniciando soma_count; colunas: reply_count + like_count + ' + repost_column)
    tmp_path = None
    sums=[]; stats=Counter()
    fieldnames = headers if 'soma_count' in headers else headers + ['soma_count']
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                                         prefix='.zee_soma_', suffix='.tmp',
                                         dir=source.parent, delete=False) as stream:
            tmp_path=Path(stream.name)
            writer=csv.DictWriter(stream, fieldnames=fieldnames, delimiter=sep,
                                  lineterminator='\n', extrasaction='raise')
            writer.writeheader()
            for i, row in enumerate(reader(source), 1):
                amounts=[]
                for col in ('reply_count', 'like_count', repost_column):
                    number, empty = parse_count(row.get(col,''), col, i)
                    amounts.append(number)
                    stats['celulas_vazias_tratadas_como_zero']+=int(empty)
                total=sum(amounts)
                row['soma_count']=str(total)
                writer.writerow(row)
                sums.append(total)
                stats['publicacoes']+=1
            stream.flush()
            os.fsync(stream.fileno())
        backup=source.with_name(source.name+'.bak')
        if backup.exists():
            backup=source.with_name(source.name + '.' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.bak')
        shutil.copy2(source, backup)
        log.say('Backup do arquivo original criado: '+str(backup))
        os.replace(tmp_path, source)
        tmp_path=None
        log.say(f'soma_count gravada no CSV original: {source}; {stats["publicacoes"]} linhas.')
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return {'operacao':'soma_simples','entrada_antes':input_stats,
            'entrada_depois':summarize_file(source),'backup':str(backup),
            'colunas_utilizadas':['reply_count','like_count',repost_column],
            'coluna_criada':'soma_count','resultado':dict(stats),
            'soma_total':sum(sums),'menor_soma':min(sums) if sums else None,
            'maior_soma':max(sums) if sums else None,
            'regra':'Soma de três contagens por linha; células vazias equivalem a zero; '
                    'valores não inteiros ou negativos causam falha sem alterar o arquivo original.'}


def metadata_rank(source, folder, log):
    """Exporta até 50 linhas com maior soma_count, mantendo todos os campos."""
    source=Path(source)
    headers=csv_headers(source)
    if 'soma_count' not in headers:
        raise ValueError('Coluna soma_count não encontrada. Execute antes Metadados > Soma Simples.')
    rows=[]
    for i,row in enumerate(reader(source),1):
        score, empty=parse_count(row.get('soma_count',''), 'soma_count', i)
        if empty:
            raise ValueError(f'Registro {i}: soma_count vazia. Execute a Soma Simples novamente.')
        rows.append((score,i,row))
    rows.sort(key=lambda x:(-x[0],x[1]))
    best=rows[:50]
    dest=folder/(source.stem+'_top50_posts.csv')
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=headers,delimiter=detectar_delimitador(source),lineterminator='\n')
        writer.writeheader()
        for score,i,row in best:
            writer.writerow(row)
    log.say(f'Ranqueamento de postagens: {len(best)} de {len(rows)} linhas -> {dest}')
    return {'operacao':'ranking_posts','entrada':summarize_file(source),
            'saida':str(dest),'publicacoes_lidas':len(rows),'publicacoes_exportadas':len(best),
            'maior_soma':best[0][0] if best else None,'menor_soma_top':best[-1][0] if best else None,
            'regra':'Ordem decrescente de soma_count. Empates preservam a ordem original. '
                    'Todas as colunas originais são preservadas, sem deduplicar posts.'}


def extract_hashtags(cell):
    """Extrai as tags de uma única célula, sem procurar hashtags no body."""
    raw=(cell or '').strip()
    if not raw: return []
    tokens=[]
    if raw.startswith('['):
        try:
            parsed=json.loads(raw)
            if isinstance(parsed,list):
                for item in parsed:
                    if isinstance(item,str): tokens.append(item)
                    elif isinstance(item,dict):
                        tokens.append(str(item.get('text') or item.get('tag') or item.get('hashtag') or ''))
            else:
                tokens=[raw]
        except json.JSONDecodeError:
            tokens=[raw]
    else:
        tokens=[raw]
    tags=[]
    for token in tokens:
        # Listas simples separadas por vírgula, ponto e vírgula, espaços ou #.
        for match in re.finditer(r'#?([^\W_][\w]*)', token, flags=re.UNICODE):
            tag=match.group(1).casefold()
            if tag: tags.append(tag)
    return tags


def metadata_hashtags(source,folder,log):
    source=Path(source)
    headers=csv_headers(source)
    column='hashtag' if 'hashtag' in headers else 'hashtags' if 'hashtags' in headers else None
    if column is None:
        raise ValueError('Não existe coluna hashtag nem hashtags no CSV.')
    counts=Counter(); stats=Counter()
    for row in reader(source):
        stats['linhas']+=1
        tags=extract_hashtags(row.get(column,''))
        if not tags: stats['linhas_sem_tags']+=1
        else: stats['linhas_com_tags']+=1
        counts.update(tags)
        stats['total_utilizacoes']+=len(tags)
    dest=folder/(source.stem+'_ranking_hashtags.csv')
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f,delimiter=';',lineterminator='\n')
        writer.writerow(['hashtag','quantidade'])
        for tag,quantity in sorted(counts.items(),key=lambda kv:(-kv[1],kv[0])):
            writer.writerow(['#'+tag,quantity])
    log.say(f'Hashtags da coluna {column}: {len(counts)} distintas; '
            f'{stats["total_utilizacoes"]} utilizações -> {dest}')
    return {'operacao':'ranking_hashtags','entrada':summarize_file(source),
            'coluna_lida':column,'saida':str(dest),'contagens':dict(stats),
            'tags_distintas':len(counts),'dez_mais_frequentes':[['#'+t,n] for t,n in counts.most_common(10)],
            'regra':'Conta ocorrências de cada hashtag na coluna específica, inclusive repetições '
                    'dentro de uma mesma célula; sem deduplicar posts. '
                    'Ignora diferença entre maiúsculas/minúsculas; não examina body.'}



def timeline_dashboard(source, folder, log, tz_name='America/Sao_Paulo', deduplicate=True):
    """Dashboard offline: visão geral, séries dia/minuto, top posts e hashtags.

    Os dados ficam incorporados no HTML. Não gera CSVs auxiliares.
    Ranking dos posts usa soma_count quando válida; senão calcula soma das
    três colunas de engajamento. Estatísticas observadas no CSV, não na plataforma.
    """
    try:
        import plotly.graph_objects as go
        from plotly.offline import get_plotlyjs
    except ImportError as exc:
        raise ValueError('O dashboard requer Plotly: pip install plotly') from exc
    from html import escape

    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError) as exc:
        raise ValueError(f'Fuso inválido: {tz_name}') from exc
    headers = set(csv_headers(source))
    if not ({'timestamp', 'unix_timestamp'} & headers):
        raise ValueError('Não existe timestamp nem unix_timestamp no CSV.')
    tag_col = 'hashtag' if 'hashtag' in headers else 'hashtags' if 'hashtags' in headers else None
    repost_col = 'repost_count' if 'repost_count' in headers else 'retweet_count' if 'retweet_count' in headers else None

    days_count, minutes_count, tag_count, causes, date_sources = Counter(), Counter(), Counter(), Counter(), Counter()
    seen, authors = set(), set()
    top_posts = []
    total = duplicates = without_key = without_date = no_score = 0
    seq = 0
    for row in reader(source):
        total += 1
        identity = key_of(row)
        if deduplicate and identity is not None:
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
        if identity is None: without_key += 1
        seq += 1
        author = (row.get('author') or '').strip()
        if author: authors.add(author.casefold())
        timestamp, origin = parse_timestamp(row.get('timestamp',''), row.get('unix_timestamp',''))
        local = timestamp.astimezone(tz) if timestamp else None
        if local:
            days_count[local.date()] += 1
            minutes_count[local.replace(second=0,microsecond=0)] += 1
            date_sources[origin] += 1
        else:
            without_date += 1
            causes[origin] += 1
        if tag_col:
            tag_count.update(extract_hashtags(row.get(tag_col,'')))
        try:
            if row.get('soma_count','').strip():
                score, _ = parse_count(row['soma_count'], 'soma_count', seq)
            elif repost_col and {'like_count','reply_count'}.issubset(headers):
                score = sum(parse_count(row.get(c,''),c,seq)[0] for c in ('like_count','reply_count',repost_col))
            else:
                score = None
            if score is None:
                no_score += 1
            else:
                top_posts.append({'score':score, 'seq':seq, 'author':author or '(sem autor)',
                    'body':row.get('body',''), 'link':row.get('link',''),
                    'datetime':local.isoformat(timespec='seconds') if local else '',
                    'likes':row.get('like_count',''), 'reposts':row.get(repost_col,'') if repost_col else '',
                    'replies':row.get('reply_count','')})
        except ValueError:
            no_score += 1
            causes['contagem_invalida'] += 1
    if not days_count:
        raise ValueError('Não há postagens com data válida para gerar a Timeline.')
    top_posts.sort(key=lambda item: (-item['score'],item['seq']))
    top_posts = top_posts[:50]
    tags_sorted = sorted(tag_count.items(),key=lambda item:(-item[1],item[0]))
    first,last = min(days_count),max(days_count)
    span = (last-first).days + 1
    if span > 36600:
        raise ValueError('Intervalo superior a 100 anos; confira as datas.')
    dates = [first+timedelta(days=i) for i in range(span)]
    daily = [days_count.get(d,0) for d in dates]
    dated = sum(daily)
    peak = max(daily)
    peaks = [d.isoformat() for d,count in zip(dates,daily) if count==peak]
    minute_peak = max(minutes_count.values())
    day_minutes = defaultdict(dict)
    for dt,n in minutes_count.items():
        key = dt.date().isoformat()
        clock = dt.strftime('%H:%M')
        day_minutes[key][clock] = day_minutes[key].get(clock,0)+n

    def js_safe(data):
        return json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')

    fig = go.Figure(go.Scatter(x=[d.isoformat() for d in dates],y=daily,
         mode='lines',line={'color':'#4778a8','width':2.5},fill='tozeroy',
         fillcolor='rgba(71,120,168,.16)',name='Postagens',
         hovertemplate='%{x}<br><b>%{y}</b> postagens<extra></extra>'))
    fig.update_layout(template='plotly_white',height=360,margin=dict(l=55,r=20,t=20,b=50),
         xaxis=dict(title='Data de publicação',type='date',rangeslider={'visible':True}),
         yaxis=dict(title='Postagens por dia',rangemode='tozero'),font={'family':'Arial, sans-serif'})
    daily_fragment = fig.to_html(full_html=False,include_plotlyjs=False,
                                  config={'displaylogo':False,'responsive':True})
    payload = js_safe({'minute':day_minutes, 'posts':top_posts,
                       'tags':[['#'+tag,n] for tag,n in tags_sorted],
                       'available_days':sorted(day_minutes), 'default_day':peaks[0]})
    html = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZEE — Dashboard</title><script>__PLOTLY__</script><style>
:root{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:#263449;background:#f6f8fb}
*{box-sizing:border-box}body{margin:0}.shell{max-width:1140px;margin:auto;padding:27px 23px 65px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:22px}
h1{font-size:24px;margin:0}h2{font-size:19px;margin:0 0 12px}h3{font-size:15px;margin:0 0 8px}
p{color:#5e6d7d;line-height:1.55}.muted{color:#5e6d7d;font-size:13px}.eyebrow{font-size:12px;color:#68819b;margin:0 0 4px}
.tabs{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:18px;border-bottom:1px solid #dbe2ea;padding-bottom:12px}
.tabs button{background:transparent;border:1px solid transparent;color:#536478;border-radius:9px;padding:11px 14px;cursor:pointer;font:inherit;font-size:14px}
.tabs button.active{background:#e5eef8;color:#245582;border-color:#bcd1e3;font-weight:650}
.tab{display:none}.tab.active{display:block}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:17px 0}
.metric,.panel{background:#fff;border:1px solid #e0e7ef;border-radius:12px;padding:19px}.metric strong{display:block;font-size:26px;font-variant-numeric:tabular-nums}
.metric span{font-size:12px;color:#69798b}.panel{margin-bottom:15px}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
select,input{font:inherit;padding:9px;border:1px solid #cbd7e4;border-radius:8px;background:white;max-width:100%}
#minuteChart{height:385px;width:100%}.post{border-top:1px solid #e4eaf0;padding:17px 0}.post:first-child{border-top:0}
.post-head{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}.post-body{color:#344459;white-space:pre-wrap;overflow-wrap:anywhere}
.post-score{font-weight:700;color:#315f8d}.tag-row{display:grid;grid-template-columns:minmax(140px,1fr) 3fr 68px;gap:12px;align-items:center;padding:9px 0}
.tag-name{overflow-wrap:anywhere}.bar{height:9px;border-radius:20px;background:#e6edf4;overflow:hidden}.bar-fill{height:100%;background:#5689b8;border-radius:20px}
.small-link{color:#32699e}footer{font-size:12px;color:#718093;margin-top:25px}.empty{padding:18px;color:#6b7784}
@media(max-width:650px){.metrics{grid-template-columns:repeat(2,1fr)}.tag-row{grid-template-columns:1fr 1.5fr 45px;gap:6px}.shell{padding:16px}}
</style></head><body><div class="shell">
<header><div><div class="eyebrow">ZEE · Zeeschuimer / X</div><h1>Dashboard de publicações</h1></div><div class="muted">__TZ__</div></header>
<nav class="tabs" aria-label="Telas do dashboard"><button class="active" data-tab="overview">Visão geral</button><button data-tab="timeline">Timeline</button><button data-tab="posts">Posts</button><button data-tab="tags">Hashtags</button></nav>
<section id="overview" class="tab active"><h2>Visão geral do corpus</h2><p>Indicadores calculados a partir das publicações presentes no CSV.</p>
<div class="metrics"><div class="metric"><strong>__NPOSTS__</strong><span>Publicações únicas observadas</span></div><div class="metric"><strong>__AUTHORS__</strong><span>Perfis autores distintos</span></div><div class="metric"><strong>__NTAGS__</strong><span>Hashtags distintas</span></div><div class="metric"><strong>__NDAYS__</strong><span>Dias no intervalo de publicação</span></div></div>
<div class="panel"><h3>Volume de postagens por dia</h3><div class="muted">Selecione Timeline para examinar os minutos.</div>__DAILY_OVERVIEW__</div>
<div class="panel muted">Período: __FIRST__ a __LAST__. Publicações sem data válida: __INVALID__. Duplicatas desconsideradas: __DUPLICATES__.</div></section>
<section id="timeline" class="tab"><h2>Timeline</h2><p>Volume por dia e por minuto conforme a <b>data de publicação</b>, não necessariamente a data de coleta.</p>
<div class="metrics"><div class="metric"><strong>__DATED__</strong><span>Postagens com data válida</span></div><div class="metric"><strong>__NDAYS__</strong><span>Dias no intervalo</span></div><div class="metric"><strong>__MEAN__</strong><span>Média diária, incluindo zeros</span></div><div class="metric"><strong>__PEAK__</strong><span>Maior volume diário</span></div></div>
<div class="panel"><h3>Volume diário</h3>__DAILY_TIMELINE__</div>
<div class="panel"><div class="toolbar"><h3>Volume por minuto — gráfico de linha</h3><label>Selecionar dia <select id="day"></select></label></div><div id="minuteChart"></div><p class="muted">São apresentados os 1.440 minutos do dia, inclusive os minutos com zero postagens. Horários agregados no fuso selecionado.</p></div></section>
<section id="posts" class="tab"><div class="toolbar"><div><h2>Ranqueamento de postagens</h2><p>50 maiores valores de soma_count, em ordem decrescente.</p></div><span class="muted" id="postsTotal"></span></div><div id="postList" class="panel"></div></section>
<section id="tags" class="tab"><h2>Ranqueamento de hashtags</h2><p>Frequência de utilização das tags na coluna original de hashtags.</p><div class="panel"><div class="toolbar"><label>Buscar tag <input type="search" id="tagSearch" placeholder="#hashtag"></label><span class="muted" id="tagsTotal"></span></div><div id="tagList"></div></div></section>
<footer>Dashboard gerado pelo ZEE · Valores referentes somente ao arquivo analisado · As contagens não representam a totalidade das postagens da plataforma.</footer>
</div><script>
const data=__DATA__;
const $=id=>document.getElementById(id);
const el=(name,cls,text)=>{const n=document.createElement(name);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n};
const fmt=n=>Number(n).toLocaleString('pt-BR');
function switchTab(name){document.querySelectorAll('.tab').forEach(n=>n.classList.toggle('active',n.id===name));document.querySelectorAll('[data-tab]').forEach(n=>n.classList.toggle('active',n.dataset.tab===name));if(name==='timeline'){Plotly.Plots.resize($('minuteChart'));document.querySelectorAll('#timeline .js-plotly-plot').forEach(n=>Plotly.Plots.resize(n));}if(name==='overview')document.querySelectorAll('#overview .js-plotly-plot').forEach(n=>Plotly.Plots.resize(n));}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));
const allMinutes=Array.from({length:1440},(_,i)=>String(Math.floor(i/60)).padStart(2,'0')+':'+String(i%60).padStart(2,'0'));
for(const day of data.available_days){const o=el('option','',day);o.value=day;$('day').appendChild(o);}
$('day').value=data.default_day;
function minutePlot(){const date=$('day').value,map=data.minute[date]||{};
 const amounts=allMinutes.map(t=>map[t]||0);
 Plotly.react('minuteChart',[{type:'scatter',mode:'lines',x:allMinutes,y:amounts,line:{color:'#4778a8',width:2},fill:'tozeroy',fillcolor:'rgba(71,120,168,.13)',hovertemplate:date+' %{x}<br><b>%{y}</b> postagens<extra></extra>'}],{
 template:'plotly_white',margin:{l:60,r:15,t:15,b:60},xaxis:{title:'Horário (HH:mm)',type:'category',tickmode:'array',tickvals:['00:00','03:00','06:00','09:00','12:00','15:00','18:00','21:00','23:59'],rangeslider:{visible:true}},yaxis:{title:'Postagens por minuto',rangemode:'tozero'},font:{family:'Arial, sans-serif'},autosize:true},{displaylogo:false,responsive:true});}
$('day').addEventListener('change',minutePlot);minutePlot();
$('postsTotal').textContent=fmt(data.posts.length)+' postagens';
if(!data.posts.length)$('postList').appendChild(el('div','empty','Nenhum post com contagens válidas.'));
for(const [i,p] of data.posts.entries()){
 const card=el('article','post'),head=el('div','post-head'),label=el('strong','',`${i+1}. ${p.author}`),score=el('span','post-score',fmt(p.score)+' interações');head.append(label,score);card.append(head);
 if(p.datetime)card.appendChild(el('div','muted',p.datetime));card.appendChild(el('div','post-body',p.body||'(sem texto)'));
 card.appendChild(el('div','muted',`Curtidas: ${p.likes||0} · Repostagens: ${p.reposts||0} · Respostas: ${p.replies||0}`));
 if(/^https:\/\/(?:www\.)?(?:x\.com|twitter\.com)\//i.test(p.link||'')){const a=el('a','small-link','Abrir publicação');a.href=p.link;a.target='_blank';a.rel='noopener noreferrer';card.appendChild(a)}
 $('postList').appendChild(card);
}
function showTags(){const search=$('tagSearch').value.trim().toLocaleLowerCase('pt-BR');const filtered=data.tags.filter(t=>t[0].toLocaleLowerCase('pt-BR').includes(search));$('tagsTotal').textContent=fmt(filtered.length)+' hashtags';const container=$('tagList');container.replaceChildren();const biggest=data.tags.length?data.tags[0][1]:1;
 for(const [tag,quantity] of filtered.slice(0,200)){const row=el('div','tag-row'),name=el('span','tag-name',tag),bar=el('div','bar'),fill=el('div','bar-fill');fill.style.width=(100*quantity/biggest)+'%';bar.appendChild(fill);row.append(name,bar,el('span','',fmt(quantity)));container.appendChild(row)}
 if(filtered.length>200)container.appendChild(el('p','muted','Exibindo as 200 primeiras tags filtradas. Refine a busca para localizar outras.'));
 if(!filtered.length)container.appendChild(el('p','empty','Nenhuma hashtag encontrada.'));
}
$('tagSearch').addEventListener('input',showTags);showTags();
</script></body></html>'''
    replacements = {
        '__TZ__':escape(tz_name), '__NPOSTS__':f'{seq:,}'.replace(',','.'),
        '__AUTHORS__':f'{len(authors):,}'.replace(',','.'),
        '__NTAGS__':f'{len(tag_count):,}'.replace(',','.'), '__NDAYS__':str(span),
        '__FIRST__':first.isoformat(), '__LAST__':last.isoformat(),
        '__INVALID__':str(without_date),'__DUPLICATES__':str(duplicates),
        '__DATED__':str(dated),'__MEAN__':f'{dated/span:.1f}'.replace('.',','),
        '__PEAK__':str(peak), '__DAILY_OVERVIEW__':daily_fragment,
        '__DAILY_TIMELINE__':daily_fragment.replace('id="','id="tl_',1),
        '__DATA__':payload, '__PLOTLY__':get_plotlyjs()
    }
    # Cada fragmento do Plotly precisa de ID único (inclusive os scripts internos).
    # Construir figura separada é mais seguro que substituir apenas o atributo id.
    replacement_daily_2 = fig.to_html(full_html=False,include_plotlyjs=False,
                                      config={'displaylogo':False,'responsive':True})
    replacements['__DAILY_TIMELINE__']=replacement_daily_2
    for key,value in replacements.items():
        html=html.replace(key,value)
    dest=Path(folder)/'dashboard.html'
    dest.write_text(html,encoding='utf-8')
    log.say(f'Dashboard HTML criado: {dest}')
    log.say(f'Publicações: {seq} únicas, {dated} datadas; duplicatas: {duplicates}; tags: {len(tag_count)}; posts ranqueados: {len(top_posts)}.')
    log.say('Dashboard sem exportação de CSVs auxiliares; ranking de posts calculado quando soma_count está ausente.')
    return {
        'operacao':'dashboard_integrado', 'entrada':summarize_file(source),
        'arquivos_saida':{'dashboard_html':str(dest)},
        'parametros':{'timezone':tz_name,'deduplicacao':deduplicate,'unidade_tempo':'dia e minuto de publicação',
            'ranking_posts':'soma_count ou reply_count + like_count + repost_count/retweet_count',
            'ranking_hashtags':f'coluna {tag_col}' if tag_col else 'coluna não encontrada',
            'exportacao_csv_auxiliar':False},
        'contagens':{'linhas_lidas':total,'publicacoes_unicas':seq,'duplicatas_ignoradas':duplicates,
            'registros_sem_chave_confiavel':without_key,'postagens_datadas':dated,
            'datas_ausentes_invalidas':without_date,'autores_distintos':len(authors),
            'hashtags_distintas':len(tag_count),'posts_ranqueados':len(top_posts),
            'posts_sem_pontuacao_valida':no_score,'origem_datas':dict(date_sources),
            'avisos':dict(causes),'dias_no_intervalo':span},
        'resumo':{'primeiro_dia':first.isoformat(),'ultimo_dia':last.isoformat(),
            'maior_volume_diario':peak,'dias_pico':peaks,'maior_volume_minuto':minute_peak,
            'mais_utilizadas':[['#'+tag,n] for tag,n in tags_sorted[:10]]},
        'observacao':'Datas representam a PUBLICAÇÃO, não a coleta. A interface funciona sem conexão à internet.'}

def execute(kind,inp,out_base,recursive=False,self_loops=False,mode='static',timezone_name='America/Sao_Paulo'):
    kind_dir = {'merge':'merge','process':'rede_mencoes_'+mode,
                'sum':'metadados_soma','rank':'metadados_ranking_posts',
                'hashtags':'metadados_ranking_hashtags', 'timeline':'timeline'}[kind]
    folder=unique_run_dir(out_base,kind_dir)
    log=RunLogger(folder/'execucao.log.txt')
    report={'execucao':{'operacao':kind,'modo':mode if kind=='process' else None,
            'inicio_utc':datetime.now(timezone.utc).isoformat(),'pasta':str(folder),
            'versao':'0.8','status':'EM_ANDAMENTO'}}
    try:
        log.say(f'INÍCIO: {kind}/{mode}; origem={inp}; destino={folder}')
        if kind=='merge':
            report['resultado']=merge(inp,folder/'twitter_consolidado.csv',recursive,log)
        elif kind=='process':
            report['resultado']=process(inp,folder/(Path(inp).stem+'_rede'),self_loops,log,mode)
        elif kind=='sum':
            report['resultado']=metadata_sum(inp,folder,log)
        elif kind=='rank':
            report['resultado']=metadata_rank(inp,folder,log)
        elif kind=='hashtags':
            report['resultado']=metadata_hashtags(inp,folder,log)
        elif kind=='timeline':
            report['resultado']=timeline_dashboard(inp,folder,log,tz_name=timezone_name)
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
            'operacao':kind,'modo':mode if kind=='process' else None,
            'entrada':str(inp),'recursive':recursive,'self_loops':self_loops,
            'pasta_saida':str(folder),'timezone':timezone_name},indent=2,ensure_ascii=False),encoding='utf-8')
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

    def all_csv():
        files=listar_csvs(folder)
        generated=sorted(output.glob('merge_*/twitter_consolidado.csv')) if output.exists() else []
        return files+generated

    while True:
        files=listar_csvs(folder)
        print('\n' + '='*54)
        print('            ZEE v0.8 — ZEESCHUIMER / X')
        print('='*54)
        print('\n  [1] Merge\n')
        print('  [2] Geração de Redes\n')
        print('  [3] Metadados\n')
        print('  [4] Timeline — postagens por dia e minuto\n')
        print('  [5] Sair\n')
        print('-'*54)
        try:
            choice=input('Escolha: ').strip()
            if choice=='5': return
            if choice=='1':
                if len(files)<2:
                    print('Coloque pelo menos dois CSVs na pasta do script.'); continue
                dest=execute('merge',folder,output)
                print(f'Resultados em: {dest}')
            elif choice=='2':
                while True:
                    print('\nGERAÇÃO DE REDES')
                    print('  [1] Rede de Menções - Estática\n')
                    print('  [2] Rede de Menções - Temporal\n')
                    print('  [0] Voltar\n')
                    selection=input('Escolha: ').strip()
                    if selection=='0': break
                    if selection not in ('1','2'):
                        print('Opção inválida.'); continue
                    selected=escolher_csv(all_csv())
                    if selected is None: continue
                    mode='static' if selection=='1' else 'temporal'
                    dest=execute('process',selected,output,mode=mode)
                    print(f'Resultados em: {dest}')
            elif choice=='3':
                while True:
                    print('\nMETADADOS')
                    print('  [1] Soma Simples — acrescentar soma_count ao CSV escolhido\n')
                    print('  [2] Ranqueamento Post — 50 maiores soma_count\n')
                    print('  [3] Ranqueamento Tag — frequência de hashtags\n')
                    print('  [0] Voltar\n')
                    selection=input('Escolha: ').strip()
                    if selection=='0': break
                    kinds={'1':'sum','2':'rank','3':'hashtags'}
                    if selection not in kinds:
                        print('Opção inválida.'); continue
                    selected=escolher_csv(all_csv())
                    if selected is None: continue
                    if selection=='1':
                        print(f'ATENÇÃO: a coluna soma_count será gravada no CSV selecionado:\n{selected}')
                        print('Será criado um backup .bak antes da substituição.')
                        confirmation=input('Confirmar alteração do CSV? [s/N]: ').strip().lower()
                        if confirmation not in ('s','sim'):
                            print('Operação cancelada.'); continue
                    dest=execute(kinds[selection],selected,output)
                    print(f'Resultados em: {dest}')
            elif choice=='4':
                selected=escolher_csv(all_csv())
                if selected is None: continue
                print('\nTimeline por DATA DE PUBLICAÇÃO (não data de coleta).')
                print('Fuso padrão: America/Sao_Paulo. Datas serão agrupadas por dia e minuto locais.')
                tz_choice=input('Fuso IANA [Enter = America/Sao_Paulo]: ').strip() or 'America/Sao_Paulo'
                dest=execute('timeline',selected,output,timezone_name=tz_choice)
                print(f'\nDashboard em: {dest}\n')
            else:
                print('Opção inválida.')
        except (EOFError,KeyboardInterrupt):
            print('\nInterrompido.'); return
        except Exception as exc:
            print(f'Erro: {exc}. Confira o relatório e o log em output/.',file=sys.stderr)


def main():
    if len(sys.argv)==1: return menu_interativo()
    p=argparse.ArgumentParser(description='ZEE v0.8 — merge, redes de menções, metadados e Timeline')
    sub=p.add_subparsers(dest='command',required=True)
    m=sub.add_parser('merge',help='Consolidar CSVs Zeeschuimer')
    m.add_argument('folder'); m.add_argument('-o','--output',required=True)
    m.add_argument('--recursive',action='store_true')
    q=sub.add_parser('process',help='Gerar rede de menções estática ou temporal')
    q.add_argument('csv'); q.add_argument('-o','--output',required=True)
    q.add_argument('--mode',choices=['static','temporal'],default='static')
    q.add_argument('--self-loops',action='store_true')
    for command,help_text in [('sum','Somar as contagens e alterar CSV com backup'),
                              ('rank','Exportar 50 posts por soma_count'),
                              ('hashtags','Exportar frequência de hashtags')]:
        item=sub.add_parser(command,help=help_text)
        item.add_argument('csv'); item.add_argument('-o','--output',required=True)
    t=sub.add_parser('timeline',help='Dashboard HTML integrado (Timeline, Posts e Hashtags)')
    t.add_argument('csv'); t.add_argument('-o','--output',required=True)
    t.add_argument('--timezone',default='America/Sao_Paulo',help='Fuso IANA para agregação diária')
    args=p.parse_args()
    try:
        if args.command=='sum':
            print(f'ATENÇÃO: o CSV será modificado com backup .bak: {args.csv}')
        folder=execute(args.command,args.folder if args.command=='merge' else args.csv,
                       args.output,recursive=getattr(args,'recursive',False),
                       self_loops=getattr(args,'self_loops',False),mode=getattr(args,'mode','static'),timezone_name=getattr(args,'timezone','America/Sao_Paulo'))
        print(f'Resultados em: {folder}')
    except Exception as exc:
        p.exit(1,f'Erro: {exc}\n')


if __name__=='__main__':
    main()
