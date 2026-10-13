# Onde Morar Em SP

**https://dandesdev.github.io/onde-morar-em-sp/**

Escolha o lugar para onde você vai todo dia em São Paulo (trabalho, faculdade, escola) e veja, quadra a quadra, onde vale a pena morar:

- **Tempo porta a porta** até o destino de ônibus, metrô e trem, calculado no seu navegador.
- **Segurança na rua**, **árvores e verde**, **comércio a pé** (mercado, padaria, farmácia, praça, pet) e **ruas calmas**, com pesos que você ajusta.
- **Alertas** onde os dados são escassos ou provavelmente subnotificados.
- **Meus imóveis**: guarde anúncios (aluguel + condomínio + IPTU, quartos, vagas, área, pet, mobília) e veja cada um no mapa com o tempo até o destino. Fica salvo só no seu navegador.

Fontes, método e limitações: [fontes.html](https://dandesdev.github.io/onde-morar-em-sp/fontes.html).

## Dados

Só dados públicos: SSP-SP (boletins de ocorrência com coordenadas, 2022 a ago/2026), Prefeitura de São Paulo / GeoSampa (linhas de ônibus, metrô e trem, árvores, iluminação, feiras, praças, classificação viária, distritos), Copernicus Sentinel-2 (vegetação, jul/2026) e OpenStreetMap (comércio e nomes de bairros, out/2026).

Contém dados do OpenStreetMap, © colaboradores do OpenStreetMap, sob a Open Database License (ODbL). Contém dados modificados do Copernicus Sentinel (2026).

## Como funciona

Site estático, sem servidor:

- `index.html`, `app.js`, `style.css`: interface (Leaflet).
- `router.js`: Web Worker que calcula o tempo de transporte de cada quadrado até o destino sobre a rede em `data/graph.bin.gz`.
- `data/`: dados já processados (os arquivos `.gz` são descompactados no navegador). Não ficam no git: são gerados pelo pipeline e publicados pelo GitHub Actions.
- `pipeline/`: download e processamento dos dados (`update.py` faz tudo; veja o cabeçalho de cada script).

## Atualização automática

O workflow [Atualizar dados](.github/workflows/update-data.yml) roda toda segunda-feira às 06:00 (horário de São Paulo) e também pode ser disparado à mão na aba Actions. Ele baixa as fontes, refaz os dados, confere se os números fazem sentido (incluindo tempos de trajeto conhecidos) e só então publica. Se uma fonte estiver fora do ar, usa a última versão boa dela. A data de cada atualização fica em `pipeline/last_update.json` e no próprio site.

Para rodar localmente: `pip install -r pipeline/requirements.txt` e `python -P pipeline/update.py` (dados em `work/` e `data/`; sirva a pasta com `python -m http.server`).

Compartilhe um destino com `?d=lat,lon&n=Nome` (acrescente `&s=l6` para o cenário com a Linha 6 operando).
