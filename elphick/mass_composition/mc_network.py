import logging
import webbrowser
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import matplotlib
import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from matplotlib import pyplot as plt
from matplotlib.colors import ListedColormap, LinearSegmentedColormap
import matplotlib.cm as cm
import seaborn as sns

from plotly.subplots import make_subplots

from elphick.mass_composition import MassComposition
from elphick.mass_composition.layout import digraph_linear_layout
from elphick.mass_composition.mc_node import MCNode, NodeType
from elphick.mass_composition.plot import parallel_plot, comparison_plot
from elphick.mass_composition.utils.geometry import midpoint
from elphick.mass_composition.utils.pd_utils import column_prefix_counts, column_prefixes


class MCNetwork(nx.DiGraph):
    def __init__(self, **attr):
        super().__init__(**attr)
        self._logger: logging.Logger = logging.getLogger(__class__.__name__)

    @classmethod
    def from_streams(cls, streams: List[MassComposition], name: Optional[str] = 'Flowsheet') -> 'MCNetwork':
        """Instantiate from a list of objects

        Args:
            streams: List of MassComposition objects
            name: name of the network

        Returns:

        """

        streams: List[MassComposition] = cls._check_indexes(streams)
        bunch_of_edges: List = []
        for stream in streams:
            if stream.nodes is None:
                raise KeyError(f'Stream {stream.name} does not have the node property set')
            nodes = stream.nodes

            # add the objects to the edges
            bunch_of_edges.append((nodes[0], nodes[1], {'mc': stream}))

        graph = cls(name=name)
        graph.add_edges_from(bunch_of_edges)
        d_node_objects: Dict = {}
        for node in graph.nodes:
            d_node_objects[node] = MCNode(node_id=int(node))
        nx.set_node_attributes(graph, d_node_objects, 'mc')

        for node in graph.nodes:
            d_node_objects[node].inputs = [graph.get_edge_data(e[0], e[1])['mc'] for e in graph.in_edges(node)]
            d_node_objects[node].outputs = [graph.get_edge_data(e[0], e[1])['mc'] for e in graph.out_edges(node)]

        graph = nx.convert_node_labels_to_integers(graph)
        # update the temporary nodes on the mc object property to match the renumbered integers
        for node1, node2, data in graph.edges(data=True):
            data['mc'].nodes = [node1, node2]

        return graph

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame,
                       name: Optional[str] = 'Flowsheet',
                       mc_name_col: Optional[str] = None) -> 'MCNetwork':
        """Instantiate from a DataFrame

        Args:
            df: The DataFrame
            name: name of the network
            mc_name_col: The column specified contains the names of objects to create.
              If None the DataFrame is assumed to be wide and the mc objects will be extracted from column prefixes.

        Returns:

        """
        logger: logging.Logger = logging.getLogger(__class__.__name__)

        res: List = []
        index_names: List = []
        if mc_name_col:
            if mc_name_col in df.index.names:
                index_names = df.index.names
                df.reset_index(mc_name_col, inplace=True)
            if mc_name_col not in df.columns:
                raise KeyError(f'{mc_name_col} is not in the columns or indexes.')
            names = df[mc_name_col].unique()
            for obj_name in names:
                res.append(MassComposition(
                    data=df.query(f'{mc_name_col} == @obj_name')[[col for col in df.columns if col != mc_name_col]],
                    name=obj_name))
            if index_names:  # reinstate the index on the original dataframe
                df.reset_index(inplace=True)
                df.set_index(index_names, inplace=True)
        else:
            # wide case - find prefixes where there are at least 3 columns
            prefix_counts = column_prefix_counts(df.columns)
            prefix_cols = column_prefixes(df.columns)
            for prefix, n in prefix_counts.items():
                if n >= 3:
                    logger.info(f"Creating object for {prefix}")
                    cols = prefix_cols[prefix]
                    res.append(MassComposition(
                        data=df[[col for col in df.columns if col in cols]].rename(
                            columns={col: col.replace(f'{prefix}_', '') for col in df.columns}),
                        name=prefix))

        return cls().from_streams(streams=res, name=name)

    @property
    def balanced(self) -> bool:
        bal_vals: List = [self.nodes[n]['mc'].balanced for n in self.nodes]
        bal_vals = [bv for bv in bal_vals if bv is not None]
        return all(bal_vals)

    @property
    def edge_status(self) -> Tuple:
        d_edge_status_ok: Dict = {}
        d_failing_edges: Dict = {}
        for u, v, data in self.edges(data=True):
            d_edge_status_ok[data['mc'].name] = data['mc'].status.ok
            if not data['mc'].status.ok:
                d_failing_edges[data['mc'].name] = data['mc'].status.failing_components
        return all(d_edge_status_ok.values()), d_failing_edges

    def get_edge_by_name(self, name: str) -> MassComposition:
        """Get the MC object from the network by its name

        Args:
            name: The string name of the MassComposition object stored on an edge in the network.

        Returns:

        """

        res: Optional[MassComposition] = None
        for u, v, a in self.edges(data=True):
            if a['mc'].name == name:
                res = a['mc']

        if not res:
            raise ValueError(f"The specified name: {name} is not found on the network.")

        return res

    def get_edge_names(self) -> List[str]:
        """Get the names of the MC objects on the edges

        Returns:

        """

        res: List = []
        for u, v, a in self.edges(data=True):
            res.append(a['mc'].name)
        return res

    def get_input_edges(self) -> List[MassComposition]:
        """Get the input (feed) edge objects

        Returns:
            List of MassComposition objects
        """

        degrees = [d for n, d in self.degree()]
        res: List[MassComposition] = [d['mc'] for u, v, d in self.edges(data=True) if degrees[u] == 1]
        return res

    def get_output_edges(self) -> List[MassComposition]:
        """Get the output (product) edge objects

        Returns:
            List of MassComposition objects
        """

        degrees = [d for n, d in self.degree()]
        res: List[MassComposition] = [d['mc'] for u, v, d in self.edges(data=True) if degrees[v] == 1]
        return res

    def get_column_formats(self, columns: List[str], strip_percent: bool = False) -> Dict[str, str]:
        """

        Args:
            columns: The columns to lookup format strings for
            strip_percent: If True remove the leading % symbol from the format (for plotly tables)

        Returns:

        """
        variables = self.get_input_edges()[0].variables
        d_format: Dict = {}
        for col in columns:
            for v in variables.vars.variables:
                if col in [v.column_name, v.name]:
                    d_format[col] = v.format
                    if strip_percent:
                        d_format[col] = d_format[col].strip('%')

        return d_format

    def report(self, apply_formats: bool = False) -> pd.DataFrame:
        """Summary Report

        Total Mass and weight averaged composition
        Returns:

        """
        chunks: List[pd.DataFrame] = []
        for n, nbrs in self.adj.items():
            for nbr, eattr in nbrs.items():
                chunks.append(eattr['mc'].aggregate().assign(name=eattr['mc'].name))
        rpt: pd.DataFrame = pd.concat(chunks, axis='index').set_index('name')
        if apply_formats:
            fmts: Dict = self.get_column_formats(rpt.columns)
            for k, v in fmts.items():
                rpt[k] = rpt[k].apply((v.replace('%', '{:,') + '}').format)
        return rpt

    def imbalance_report(self, node: int):
        mc_node: MCNode = self.nodes[node]['mc']
        rpt: Path = mc_node.imbalance_report()
        webbrowser.open(str(rpt))

    def query(self, mc_name: str, queries: Dict) -> 'MCNetwork':
        """Query/filter across the network

        The queries provided will be applied to the MassComposition object in the network with the mc_name.
        The indexes for that result are then used to filter the other edges of the network.

        Args:
            mc_name: The name of the MassComposition object in the network to which the first filter to be applied.
            queries: The query or queries to apply to the object with mc_name.

        Returns:

        """

        mc_obj_ref: MassComposition = self.get_edge_by_name(mc_name).query(queries=queries)
        # TODO: This construct limits us to filtering along a single dimension only
        coord: str = list(queries.keys())[0]
        index = mc_obj_ref.data[coord]

        # iterate through all other objects on the edges and filter them to the same indexes
        mc_objects: List[MassComposition] = []
        for u, v, a in self.edges(data=True):
            if a['mc'].name == mc_name:
                mc_objects.append(mc_obj_ref)
            else:
                mc_obj: MassComposition = deepcopy(self.get_edge_by_name(a['mc'].name))
                mc_obj._data = mc_obj._data.sel({coord: index.values})
                mc_objects.append(mc_obj)

        res: MCNetwork = MCNetwork.from_streams(mc_objects)

        return res

    def get_node_input_outputs(self, node) -> Tuple:
        in_edges = self.in_edges(node)
        in_mc = [self.get_edge_data(oe[0], oe[1])['mc'] for oe in in_edges]
        out_edges = self.out_edges(node)
        out_mc = [self.get_edge_data(oe[0], oe[1])['mc'] for oe in out_edges]
        return in_mc, out_mc

    def plot(self, orientation: str = 'horizontal') -> plt.Figure:
        """Plot the network with matplotlib

        Args:
            orientation: 'horizontal'|'vertical' network layout

        Returns:

        """

        hf, ax = plt.subplots()
        # pos = nx.spring_layout(self, seed=1234)
        pos = digraph_linear_layout(self, orientation=orientation)

        edge_labels: Dict = {}
        edge_colors: List = []
        node_colors: List = []

        for node1, node2, data in self.edges(data=True):
            edge_labels[(node1, node2)] = data['mc'].name
            if data['mc'].status.ok:
                edge_colors.append('gray')
            else:
                edge_colors.append('red')

        for n in self.nodes:
            if self.nodes[n]['mc'].node_type == NodeType.BALANCE:
                if self.nodes[n]['mc'].balanced:
                    node_colors.append('green')
                else:
                    node_colors.append('red')
            else:
                node_colors.append('gray')

        nx.draw(self, pos=pos, ax=ax, with_labels=True, font_weight='bold',
                node_color=node_colors, edge_color=edge_colors)

        nx.draw_networkx_edge_labels(self, pos=pos, ax=ax, edge_labels=edge_labels, font_color='black')
        ax.set_title(self._plot_title(html=False), fontsize=10)

        return hf

    def plot_balance(self, facet_col_wrap: int = 3,
                     color: Optional[str] = 'node') -> go.Figure:
        """Plot input verus output across all nodes in the network

        Args:
            facet_col_wrap: the number of subplots per row before wrapping
            color: The optional variable to color by. If None color will be by Node

        Returns:

        """
        # prepare the data
        chunks_in: List = []
        chunks_out: List = []
        for n in self.nodes:
            if self.nodes[n]['mc'].node_type == NodeType.BALANCE:
                chunks_in.append(self.nodes[n]['mc'].add('in').assign(**{'direction': 'in', 'node': n}))
                chunks_out.append(self.nodes[n]['mc'].add('out').assign(**{'direction': 'out', 'node': n}))
        df_in: pd.DataFrame = pd.concat(chunks_in)
        index_names = ['direction', 'node'] + df_in.index.names
        df_in = df_in.reset_index().melt(id_vars=index_names)
        df_out: pd.DataFrame = pd.concat(chunks_out).reset_index().melt(id_vars=index_names)
        df_plot: pd.DataFrame = pd.concat([df_in, df_out])
        df_plot = df_plot.set_index(index_names + ['variable'], append=True).unstack(['direction'])
        df_plot.columns = df_plot.columns.droplevel(0)
        df_plot.reset_index(level=list(np.arange(-1, -len(index_names) - 1, -1)), inplace=True)
        df_plot['node'] = pd.Categorical(df_plot['node'])

        # plot
        fig = comparison_plot(data=df_plot,
                              x='in', y='out',
                              facet_col_wrap=facet_col_wrap,
                              color=color)
        return fig

    def plot_network(self, orientation: str = 'horizontal') -> go.Figure:
        """Plot the network with plotly

        Args:
            orientation: 'horizontal'|'vertical' network layout

        Returns:

        """
        # pos = nx.spring_layout(self, seed=1234)
        pos = digraph_linear_layout(self, orientation=orientation)

        edge_traces, node_trace, edge_annotation_trace = self._get_scatter_node_edges(pos)
        title = self._plot_title()

        fig = go.Figure(data=[*edge_traces, node_trace, edge_annotation_trace],
                        layout=go.Layout(
                            title=title,
                            titlefont_size=16,
                            showlegend=False,
                            hovermode='closest',
                            margin=dict(b=20, l=5, r=5, t=40),
                            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                            paper_bgcolor='rgba(0,0,0,0)',
                            plot_bgcolor='rgba(0,0,0,0)'
                        ),
                        )
        # for k, d_args in edge_annotations.items():
        #     fig.add_annotation(x=d_args['pos'][0], y=d_args['pos'][1], text=k, textangle=d_args['angle'])

        return fig

    def plot_sankey(self,
                    width_var: str = 'mass_wet',
                    color_var: Optional[str] = None,
                    edge_colormap: Optional[str] = 'copper_r',
                    vmin: Optional[float] = None,
                    vmax: Optional[float] = None,
                    ) -> go.Figure:
        """Plot the Network as a sankey

        Args:
            width_var: The variable that determines the sankey width
            color_var: The optional variable that determines the sankey edge color
            edge_colormap: The optional colormap.  Used with color_var.
            vmin: The value that maps to the minimum color
            vmax: The value that maps to the maximum color

        Returns:

        """
        d_sankey: Dict = self._generate_sankey_args(color_var, edge_colormap, width_var, vmin, vmax)
        node, link = self._get_sankey_node_link_dicts(d_sankey)
        fig = go.Figure(data=[go.Sankey(node=node, link=link)])
        title = self._plot_title()
        fig.update_layout(title_text=title, font_size=10)
        return fig

    def table_plot(self,
                   plot_type: str = 'sankey',
                   cols_exclude: Optional[List] = None,
                   table_pos: str = 'left',
                   table_area: float = 0.4,
                   table_header_color: str = 'cornflowerblue',
                   table_odd_color: str = 'whitesmoke',
                   table_even_color: str = 'lightgray',
                   sankey_width_var: str = 'mass_wet',
                   sankey_color_var: Optional[str] = None,
                   sankey_edge_colormap: Optional[str] = 'copper_r',
                   sankey_vmin: Optional[float] = None,
                   sankey_vmax: Optional[float] = None,
                   network_orientation: Optional[str] = 'horizontal'
                   ) -> go.Figure:
        """Plot with table of edge averages

        Args:
            plot_type: The type of plot ['sankey', 'network']
            cols_exclude: List of columns to exclude from the table
            table_pos: Position of the table ['left', 'right', 'top', 'bottom']
            table_area: The proportion of width or height to allocate to the table [0, 1]
            table_header_color: Color of the table header
            table_odd_color: Color of the odd table rows
            table_even_color: Color of the even table rows
            sankey_width_var: If plot_type is sankey, the variable that determines the sankey width
            sankey_color_var: If plot_type is sankey, the optional variable that determines the sankey edge color
            sankey_edge_colormap: If plot_type is sankey, the optional colormap.  Used with sankey_color_var.
            sankey_vmin: The value that maps to the minimum color
            sankey_vmax: The value that maps to the maximum color
            network_orientation: The orientation of the network layout 'vertical'|'horizontal'

        Returns:

        """

        valid_plot_types: List[str] = ['sankey', 'network']
        if plot_type not in valid_plot_types:
            raise ValueError(f'The supplied plot_type is not in {valid_plot_types}')

        valid_table_pos: List[str] = ['top', 'bottom', 'left', 'right']
        if table_pos not in valid_table_pos:
            raise ValueError(f'The supplied table_pos is not in {valid_table_pos}')

        d_subplot, d_table, d_plot = self._get_position_kwargs(table_pos, table_area, plot_type)

        fig = make_subplots(**d_subplot, print_grid=False)

        df: pd.DataFrame = self.report().reset_index()
        if cols_exclude:
            df = df[[col for col in df.columns if col not in cols_exclude]]
        fmt: List[str] = ['%s'] + list(self.get_column_formats(df.columns, strip_percent=True).values())
        column_widths = [2] + [1] * (len(df.columns) - 1)

        fig.add_table(
            header=dict(values=list(df.columns),
                        fill_color=table_header_color,
                        align='center',
                        font=dict(color='black', size=12)),
            columnwidth=column_widths,
            cells=dict(values=df.transpose().values.tolist(),
                       align='left', format=fmt,
                       fill_color=[
                           [table_odd_color if i % 2 == 0 else table_even_color for i in range(len(df))] * len(
                               df.columns)]),
            **d_table)

        if plot_type == 'sankey':
            d_sankey: Dict = self._generate_sankey_args(sankey_color_var,
                                                        sankey_edge_colormap,
                                                        sankey_width_var,
                                                        sankey_vmin,
                                                        sankey_vmax)
            node, link = self._get_sankey_node_link_dicts(d_sankey)
            fig.add_trace(go.Sankey(node=node, link=link), **d_plot)

        elif plot_type == 'network':
            # pos = nx.spring_layout(self, seed=1234)
            pos = digraph_linear_layout(self, orientation=network_orientation)

            edge_traces, node_trace, edge_annotation_trace = self._get_scatter_node_edges(pos)
            fig.add_traces(data=[*edge_traces, node_trace, edge_annotation_trace], **d_plot)

            fig.update_layout(showlegend=False, hovermode='closest',
                              xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                              yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
                              paper_bgcolor='rgba(0,0,0,0)',
                              plot_bgcolor='rgba(0,0,0,0)'
                              )

        title = self._plot_title(compact=True)
        fig.update_layout(title_text=title, font_size=12)

        return fig

    def to_dataframe(self,
                     names: Optional[str] = None):
        """Return a tidy dataframe

        Adds the mc name to the index so indexes are unique.

        Args:
            names: Optional List of names of MassComposition objects (network edges) for export

        Returns:

        """
        chunks: List[pd.DataFrame] = []
        for u, v, data in self.edges(data=True):
            if (names is None) or ((names is not None) and (data['mc'].name in names)):
                chunks.append(data['mc'].data.mc.to_dataframe().assign(name=data['mc'].name))
        return pd.concat(chunks, axis='index').set_index('name', append=True)

    def plot_parallel(self,
                      names: Optional[str] = None,
                      color: Optional[str] = None,
                      vars_include: Optional[List[str]] = None,
                      vars_exclude: Optional[List[str]] = None,
                      title: Optional[str] = None,
                      include_dims: Optional[Union[bool, List[str]]] = True,
                      plot_interval_edges: bool = False) -> go.Figure:
        """Create an interactive parallel plot

        Useful to explore multidimensional data like mass-composition data

        Args:
            names: Optional List of Names to plot
            color: Optional color variable
            vars_include: Optional List of variables to include in the plot
            vars_exclude: Optional List of variables to exclude in the plot
            title: Optional plot title
            include_dims: Optional boolean or list of dimension to include in the plot.  True will show all dims.
            plot_interval_edges: If True, interval edges will be plotted instead of interval mid

        Returns:

        """
        df: pd.DataFrame = self.to_dataframe(names=names)

        if not title and hasattr(self, 'name'):
            title = self.name

        fig = parallel_plot(data=df, color=color, vars_include=vars_include, vars_exclude=vars_exclude, title=title,
                            include_dims=include_dims, plot_interval_edges=plot_interval_edges)
        return fig

    @staticmethod
    def _get_position_kwargs(table_pos, table_area, plot_type):
        """Helper to manage location dependencies

        Args:
            table_pos: position of the table: left|right|top|bottom
            table_width: fraction of the plot to assign to the table [0, 1]

        Returns:

        """
        name_type_map: Dict = {'sankey': 'sankey', 'network': 'xy'}
        specs = [[{"type": 'table'}, {"type": name_type_map[plot_type]}]]

        widths: Optional[List[float]] = [table_area, 1.0 - table_area]
        subplot_kwargs: Dict = {'rows': 1, 'cols': 2, 'specs': specs}
        table_kwargs: Dict = {'row': 1, 'col': 1}
        plot_kwargs: Dict = {'row': 1, 'col': 2}

        if table_pos == 'left':
            subplot_kwargs['column_widths'] = widths
        elif table_pos == 'right':
            subplot_kwargs['column_widths'] = widths[::-1]
            subplot_kwargs['specs'] = [[{"type": name_type_map[plot_type]}, {"type": 'table'}]]
            table_kwargs['col'] = 2
            plot_kwargs['col'] = 1
        else:
            subplot_kwargs['rows'] = 2
            subplot_kwargs['cols'] = 1
            table_kwargs['col'] = 1
            plot_kwargs['col'] = 1
            if table_pos == 'top':
                subplot_kwargs['row_heights'] = widths
                subplot_kwargs['specs'] = [[{"type": 'table'}], [{"type": name_type_map[plot_type]}]]
                table_kwargs['row'] = 1
                plot_kwargs['row'] = 2
            elif table_pos == 'bottom':
                subplot_kwargs['row_heights'] = widths[::-1]
                subplot_kwargs['specs'] = [[{"type": name_type_map[plot_type]}], [{"type": 'table'}]]
                table_kwargs['row'] = 2
                plot_kwargs['row'] = 1

        if plot_type == 'network':  # different arguments for different plots
            plot_kwargs = {f'{k}s': v for k, v in plot_kwargs.items()}

        return subplot_kwargs, table_kwargs, plot_kwargs

    def _generate_sankey_args(self, color_var, edge_colormap, width_var, v_min, v_max):
        rpt: pd.DataFrame = self.report()
        if color_var is not None:
            cmap = sns.color_palette(edge_colormap, as_cmap=True)
            rpt: pd.DataFrame = self.report()
            if not v_min:
                v_min = np.floor(rpt[color_var].min())
            if not v_max:
                v_max = np.ceil(rpt[color_var].max())
        if isinstance(list(self.nodes)[0], int):
            labels = [str(n) for n in list(self.nodes)]
        else:
            labels = list(self.nodes)
        # run the report for the hover data
        d_custom_data: Dict = self._rpt_to_html(df=rpt)
        source: List = []
        target: List = []
        value: List = []
        edge_custom_data = []
        edge_color: List = []
        edge_labels: List = []
        node_colors: List = []

        for n in self.nodes:
            if self.nodes[n]['mc'].node_type == NodeType.BALANCE:
                if self.nodes[n]['mc'].balanced:
                    node_colors.append('green')
                else:
                    node_colors.append('red')
            else:
                node_colors.append('blue')

        for u, v, data in self.edges(data=True):
            edge_labels.append(data['mc'].name)
            source.append(u)
            target.append(v)
            value.append(float(data['mc'].aggregate()[width_var].iloc[0]))
            edge_custom_data.append(d_custom_data[data['mc'].name])

            if color_var is not None:
                val: float = float(data['mc'].aggregate()[color_var].iloc[0])
                str_color: str = f'rgba{self._color_from_float(v_min, v_max, val, cmap)}'
                edge_color.append(str_color)
            else:
                edge_color: Optional[str] = None

        d_sankey: Dict = {'node_color': node_colors,
                          'edge_color': edge_color,
                          'edge_custom_data': edge_custom_data,
                          'edge_labels': edge_labels,
                          'labels': labels,
                          'source': source,
                          'target': target,
                          'value': value}

        return d_sankey

    @staticmethod
    def _get_sankey_node_link_dicts(d_sankey: Dict):
        node: Dict = dict(
            pad=15,
            thickness=20,
            line=dict(color="black", width=0.5),
            label=d_sankey['labels'],
            color=d_sankey['node_color'],
            customdata=d_sankey['labels']
        )
        link: Dict = dict(
            source=d_sankey['source'],  # indices correspond to labels, eg A1, A2, A1, B1, ...
            target=d_sankey['target'],
            value=d_sankey['value'],
            color=d_sankey['edge_color'],
            label=d_sankey['edge_labels'],  # over-written by hover template
            customdata=d_sankey['edge_custom_data'],
            hovertemplate='<b><i>%{label}</i></b><br />Source: %{source.customdata}<br />'
                          'Target: %{target.customdata}<br />%{customdata}'
        )
        return node, link

    def _get_scatter_node_edges(self, pos):
        # edges
        edge_color_map: Dict = {True: 'grey', False: 'red'}
        edge_annotations: Dict = {}

        edge_traces = []
        for u, v, data in self.edges(data=True):
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            edge_annotations[data['mc'].name] = {'pos': midpoint(pos[u], pos[v])}
            edge_traces.append(go.Scatter(x=[x0, x1], y=[y0, y1],
                                          line=dict(width=2, color=edge_color_map[data['mc'].status.ok]),
                                          hoverinfo='text',
                                          mode='lines',
                                          text=data['mc'].name))

        # nodes
        node_color_map: Dict = {None: 'grey', True: 'green', False: 'red'}
        node_x = []
        node_y = []
        node_color = []
        node_text = []
        for node in self.nodes():
            x, y = pos[node]
            node_x.append(x)
            node_y.append(y)
            node_color.append(node_color_map[self.nodes[node]['mc'].balanced])
            node_text.append(node)
        node_trace = go.Scatter(
            x=node_x, y=node_y,
            mode='markers+text',
            hoverinfo='none',
            marker=dict(
                color=node_color,
                size=30,
                line_width=2),
            text=node_text)

        # edge annotations
        edge_labels = list(edge_annotations.keys())
        edge_label_x = [edge_annotations[k]['pos'][0] for k, v in edge_annotations.items()]
        edge_label_y = [edge_annotations[k]['pos'][1] for k, v in edge_annotations.items()]

        edge_annotation_trace = go.Scatter(
            x=edge_label_x, y=edge_label_y,
            mode='markers',
            hoverinfo='text',
            marker=dict(
                color='grey',
                size=3,
                line_width=1),
            text=edge_labels)

        return edge_traces, node_trace, edge_annotation_trace

    def _rpt_to_html(self, df: pd.DataFrame) -> Dict:
        custom_data: Dict = {}
        fmts: Dict = self.get_column_formats(df.columns)
        for i, row in df.iterrows():
            str_data: str = '<br />'
            for k, v in dict(row).items():
                str_data += f"{k}: {v:{fmts[k][1:]}}<br />"
            custom_data[i] = str_data
        return custom_data

    @staticmethod
    def _color_from_float(vmin: float, vmax: float, val: float,
                          cmap: Union[ListedColormap, LinearSegmentedColormap]) -> Tuple[float, float, float]:
        if isinstance(cmap, ListedColormap):
            color_index: int = int((val - vmin) / ((vmax - vmin) / 256.0))
            color_index = min(max(0, color_index), 255)
            color_rgba = tuple(cmap.colors[color_index])
        elif isinstance(cmap, LinearSegmentedColormap):
            norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
            m = cm.ScalarMappable(norm=norm, cmap=cmap)
            r, g, b, a = m.to_rgba(val, bytes=True)
            color_rgba = int(r), int(g), int(b), int(a)
        else:
            NotImplementedError("Unrecognised colormap type")

        return color_rgba

    def _plot_title(self, html: bool = True, compact: bool = False):
        title = f"{self.name}<br><br><sup>Balanced: {self.balanced}<br>Edge Status OK: {self.edge_status[0]}</sup>"
        if compact:
            title = title.replace("<br><br>", "<br>").replace("<br>Edge", ", Edge")
        if not self.edge_status[0]:
            title = title.replace("</sup>", "") + f", {self.edge_status[1]}</sup>"
        if not html:
            title = title.replace('<br><br>', '\n').replace('<br>', '\n').replace('<sup>', '').replace('</sup>', '')
        return title

    @classmethod
    def _check_indexes(cls, streams):
        logger: logging.Logger = logging.getLogger(__class__.__name__)

        list_of_indexes = [s.data.to_dataframe().index for s in streams]
        types_of_indexes = [type(i) for i in list_of_indexes]
        # check the index types are consistent
        if len(set(types_of_indexes)) != 1:
            raise KeyError("stream index types are not consistent")

        # check the shapes are consistent
        if len(np.unique([i.shape for i in list_of_indexes])) != 1:
            if list_of_indexes[0].names == ['size']:
                logger.debug(f"size index detected - attempting index alignment")
                # two failure modes can be managed:
                # 1) missing coarse size fractions - can be added with zeros
                # 2) missing intermediate fractions - require interpolation to preserve mass
                df_streams: pd.DataFrame = pd.concat([s.data.to_dataframe().assign(stream=s.name) for s in streams])
                df_streams_full = df_streams.pivot(columns=['stream'])
                df_streams_full.columns.names = ['component', 'stream']
                df_streams_full.sort_index(ascending=False, inplace=True)
                stream_nans: pd.DataFrame = df_streams_full.isna().stack(level=-1)

                for stream in streams:
                    s: str = stream.name
                    tmp_nans: pd.Series = stream_nans.query('stream==@s').sum(axis=1)
                    if tmp_nans.iloc[0] > 0:
                        logging.debug(f'The {s} stream has missing coarse sizes')
                        first_zero_index = tmp_nans.loc[tmp_nans == 0].index[0]
                        if tmp_nans[tmp_nans.index <= first_zero_index].sum() > 0:
                            logging.debug(f'The {s} stream has missing sizes requiring interpolation')
                            raise NotImplementedError('Coming soon - we need interpolation!')
                        else:
                            logging.debug(f'The {s} stream has missing coarse sizes only')
                            stream_df = df_streams_full.loc[:, (slice(None), s)].droplevel(-1, axis=1).fillna(0)
                            # recreate the stream from the dataframe
                            stream.set_data(stream_df)
            else:
                raise KeyError("stream index shapes are not consistent")
        return streams
