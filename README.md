# ZEE v0.3 — Revisão da infraestrutura

Preserva **merge** e **rede dirigida usuário → usuário** do script `zee(5).py`. Não implementa hashtags, ranking, estatísticas gerais, rede de palavras ou Timeline.

## Requisitos

Python 3.10+ e `pip install networkx`.

## Uso

- Abra o menu: `python zee.py`
- Consolide: `python zee.py merge pasta_de_csv -o resultados`
- Processe: `python zee.py process arquivo.csv -o resultados`
- Opcional: `--recursive` no merge; `--self-loops` no process.

**Mudança importante:** `-o` agora designa a **pasta-base de resultados**, e não mais um único arquivo ou prefixo. Cada execução gera uma subpasta inédita. A rotina interativa lê CSVs ao lado de `zee.py` e permite processar os consolidados anteriores encontrados nas subpastas `output/merge_*`.

## Arquivos de saída

Cada subpasta inclui:
- `execucao.log.txt`: histórico de etapas, avisos e erros (inclusive traceback em falhas).
- `relatorio_processamento.txt`: resumo, métricas, critérios e registros de auditoria.
- `configuracao.json`: parâmetros usados.
- `twitter_consolidado.csv` (merge) **ou** `*_rede.gexf` e `*_rede.json` (process).

O log registra etapas e contagens agregadas, **não** o corpo de todas as publicações, para evitar arquivos gigantescos e repetição de dados pessoais. O relatório traz até dez exemplos de registros problemáticos/duplicados para depuração. **Os logs podem conter identificadores e caminhos locais**, portanto devem ser revisados antes de publicação.

## Correções e decisões metodológicas

- **IDs de usuário:** mantém a chave do grafo como `@handle` sem alterar sua topologia anterior. Adiciona auditoria de conflitos entre `author_id` e handles observados. *Não resolve historicamente* renomeações de perfis nem atribui IDs aos usuários apenas mencionados. Esses casos exigem fontes adicionais.
- **Deduplicação:** contabiliza os descartes por arquivo no merge e por conjunto no process. Mantém a primeira publicação; sem link/ID seguro, não presume equivalência. Registra até dez exemplos.
- **Datas:** usa timestamps ISO 8601 com **fuso explícito**; converte para UTC antes de comparar primeira/última ocorrência. Datas inválidas, ausentes ou sem fuso são contabilizadas, mas ignoradas nos limites temporais. Não cria Timeline nem inventa horário.
- **Repetibilidade:** relatório inclui hash SHA-256 e tamanho dos CSVs de entrada. Pastas únicas impedem substituição acidental de resultados.
- **Rede:** dirigida e ponderada, conta **eventos de interação**. Uma mesma postagem pode contribuir com vários eventos. Não equivale a relação de seguidor, aprovação ou pessoa única.

## Pendente, intencionalmente

Extração/rede de hashtags; ranqueamento de perfis; métricas aritméticas descritivas; linha temporal do volume de publicações por dia. Também permanecem pendentes WordGraph, trigramas e Gephi Timeline dinâmica.

## Ajuste temporal — v0.3 (27/10/2026)

A operação `process` produz **dois arquivos GEXF** quando há interações datadas:

1. `*_rede.gexf` — rede **estática completa**: inclui também interações sem timestamp válido, armazena datas em `first_timestamp`/`last_timestamp` (ISO 8601 UTC) e inteiros em `first_timestamp_unix`/`last_timestamp_unix` (segundos Unix). Esses atributos **não ativam**, por si sós, a Timeline.
2. `*_rede_temporal.gexf` — rede **dinâmica** GEXF 1.3 com `mode="dynamic"`, `timerepresentation="timestamp"`, `timeformat="dateTime"` e `spells` com cada instante observado de interação. Não pressupõe continuidade entre eventos. A Timeline do Gephi deve aparecer ao importar esse arquivo em um projeto vazio.

Regras e limites:
- Os timestamps da coluna `timestamp` precisam incluir fuso (`Z` ou `-03:00`, por exemplo). Datas sem fuso, inválidas ou ausentes não são usadas na rede temporal e são contadas no relatório.
- A rede dinâmica contém apenas as relações com ao menos um evento com data válida. O nó do autor também pode aparecer em instantes de postagens sem interação, desde que possua data válida.
- Se vários eventos do mesmo par ocorrerem exatamente no mesmo microssegundo, haverá um único *spell* naquele instante; o peso total ainda contabiliza todos os eventos.
- O `weight` da rede dinâmica é **agregado sobre toda a amostra**, não muda com a posição da Timeline. Para calcular volume de interações por intervalo, será necessária uma implementação específica de peso dinâmico ou agregação temporal.
- Os campos Unix são segundos desde 1970-01-01 UTC; não use esses campos como se fossem datas formatadas. Eles permitem filtragem numérica e compatibilidade com ferramentas que exigem inteiros.
- A estrutura do XML foi testada localmente; a importação na instalação específica do Gephi do usuário deve ser validada, pois implementações/versões podem ter diferenças no suporte GEXF 1.3.

No Gephi, importe preferencialmente o arquivo `*_rede_temporal.gexf` em um workspace vazio e use o painel Timeline. Para visualizar todas as interações independentemente de datas, importe `*_rede.gexf` em outro workspace.
