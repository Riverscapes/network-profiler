import csv
import networkx as nx
import ogr
from qgis.core import *
import logging
from collections import namedtuple
from shapely.geometry import *

"""
We invented a special kind of tuple to handle all the different properties of an "Edge"
"""
EdgeObj = namedtuple('EdgeObj', ['edge', 'fid'], verbose=True)

class Profile():

    def __init__(self, shpLayer, msgcallback=None):
        """
        Profile a network from startID to endID

        If no outID is specified we go and found the outflow point and use that

        :param shpLayer: The QgsVector layer to use
        :param inID:  The ID of the input network segment
        :param outID:  The ID of the output network segment (Optional) Default is none
        """
        # TODO: Could not find because those points are in two different subnetworks. Please fix your network
        # TODO: Could not find because stream flow was a problem. If you reverse your input and output then it works
        self.logger = logging.getLogger('Profile')
        self.msgcallback = msgcallback
        self.idField = "_FID_"
        self.logmsgs = []

        self.paths = []
        self.features = {}

        # Convert QgsLayer to NX graph
        self.qgsLayertoNX(shpLayer, simplify=True)

    def writeCSV(self, filename, cols=None):
        """
        Separate out the writer so we can test without writing files
        :param outdict:
        :param csv:
        :return:
        """
        results = []
        self.logInfo("Writing CSV file")
        if len(self.attr) == 0:
            self.logError("WARNING: No rows to write to CSV. Nothing done")
            return

        # Make a subset dictionary
        includedShpCols = []
        if len(cols) > 0:
            for col in cols:
                if col not in self.attr[0]['shpfields']:
                    self.logError("WARNING: Could not find column '{}' in shapefile".format(col))
                else:
                    includedShpCols.append(col)
        else:
            includedShpCols = self.attr[0]['shpfields'].keys()

        # Now just pull out the columns we need
        for node in self.attr:
            csvDict = {}

            # The ID field is not optional
            # TODO: Hardcoding "FID" might not be the best idea here
            csvDict[self.idField] = node['shpfields'][self.idField]

            # Debug gets the Wkt
            # if self.debug:
            csvDict["Wkt"] = node['shpfields']['Wkt']

            # Only some of the fields get included
            for key, val in node['shpfields'].iteritems():
                if key in includedShpCols:
                    csvDict[key] = val
            # Everything calculated gets included
            for key, val in node['calculated'].iteritems():
                csvDict[key] = val

            results.append(csvDict)


        with open(filename, 'wb') as filename:
            keys = results[0].keys()

            # pyt the keys in order
            def colSort(a, b):
                # idfield should bubble up
                item = self.attr[0]
                if a == self.idField:
                    return -1
                elif b == self.idField:
                    return 1
                # put shpfields ahead of calc fields
                elif (a in item['shpfields'] and b in item['calculated']):
                    return -1
                elif (a in item['calculated'] and b in item['shpfields']):
                    return 1
                # Sort everything else alphabetically
                elif (a in item['shpfields'] and b in item['shpfields']) or (a in item['calculated'] and b in item['calculated']):
                    if a.lower() > b.lower():
                        return 1
                    elif a.lower() < b.lower():
                        return -1
                    else:
                        return 0
                else:
                    return -1

            keys.sort(colSort)

            writer = csv.DictWriter(filename, keys)
            writer.writeheader()
            writer.writerows(results)
        self.logInfo("Done Writing CSV")

    def getPathEdgeIds(self):
        """
        Get the FIDs of all the paths in the object
        :return:
        """
        ids = []
        for path in self.paths:
            ids.append([idx[1] for idx in path])
        return ids

    def _calcfields(self, edges):
        """
        These are fields that need to be calculated.
        :param edges:
        :return:
        """
        path = []

        cummulativelength = 0
        for idx, edge in enumerate(edges):
            # Get the ID for this edge
            attrFields ={}
            attrCalc = {}
            attrFields = {k: v for k, v in self.G.get_edge_data(*edge).iteritems() if k.lower() not in ['json', 'wkb', 'wkt']}

            attrCalc = {}
            attrCalc['ProfileCalculatedLength'] = attrFields['_calc_length_']
            cummulativelength += attrCalc['ProfileCalculatedLength']
            attrCalc['ProfileCummulativeLength'] = cummulativelength
            attrCalc['ProfileID'] = idx + 1
            # Calculate length and cumulative length
            # EdgeObj = namedtuple('EdgeObj', ['EdgeTuple', 'KIndex', 'Attr', 'CalcAttr'], verbose=True)
            path.append(EdgeObj(edge, attrFields, attrCalc))

        return path


    def pathfinder(self, inID, outID=None):
        """
        Find the shortest path between two nodes or just one node and the outflow
        :param G:
        :param inID:
        :param outID:
        :return:
        """
        self.paths = []
        startEdge = self.findEdgewithID(inID)

        def prepareEdges(G, edges):
            return [(edge, G.get_edge_data(*edge).keys()) for edge in edges]

        def recursivePathFinder(edges, index=0, path=[]):
            """
            Help us find all the different paths with a given combination of nodes
            :return:
            """
            newpath = path[:]

            # Continue along a straight edge as far as we can until we end or find a fork
            while index < len(edges) and len(edges[index][1]) < 2:
                newpath.append((edges[index], edges[index][1][0]))
                index += 1

            if index >= len(edges):
                self.paths.append(newpath)
            else:
                # Here is the end or a fork
                for fid in edges[index][1]:
                    newEdge = [(edges[index], fid)]
                    recursivePathFinder(edges, index+1, newpath + newEdge)


        if not startEdge:
            raise Exception("Could not find start ID: {} in network.".format(inID))
        else:
            startPoint = startEdge.edge[1]

        if outID:
            endEdge = self.findEdgewithID(outID)
            if not endEdge:
                raise Exception("Could not find end ID: {} in network.".format(outID))
            else:
                endPoint = endEdge.edge[1]
            # Make a depth-first tree from the first headwater we find
            try:
                # Get all possible paths
                paths = [path for path in nx.all_simple_paths(self.G, source=startPoint, target=endPoint)]
                # Remove duplicate traversal paths (we need to recalc them later recursively)
                paths = [x for i, x in enumerate(paths) if i == paths.index(x)]

                # Zip up the edge pairs and add the FIDs back
                pathedges = [prepareEdges(self.G, zip(path, path[1:])) for path in paths]

                # There may be multiple paths so we need to find indices
                for edges in pathedges:
                    recursivePathFinder(edges, path=[(startEdge.edge, inID)])

            except Exception, e:
                print e.message
                raise Exception("Path not found between these two points with id: '{}' and '{}'".format(inID, outID))
        else:
            # This is a "FIND THE OUTFLOW" case where a B point isn't specified
            try:
                edges = list(nx.dfs_edges(self.G, startPoint))
                edges = prepareEdges(self.G, edges)
                recursivePathFinder(edges, path=[(startEdge.edge, inID)])

            except Exception, e:
                print e.message
                raise Exception("Path not found between input point with ID: {} and outflow point".format(inID))


    def qgsLayertoNX(self, shapelayer, simplify=True, geom_attrs=True):
        """
        THIS IS a re-purposed version of load_shp from nx
        :param shapelayer:
        :param simplify:
        :param geom_attrs:
        :return:
        """
        self.logInfo("parsing shapefile into network...")

        self.G = nx.MultiDiGraph()
        self.logInfo("Shapefile successfully parsed into directed network")

        for f in shapelayer.getFeatures():

            flddata = f.attributes()
            fields = [str(fi.name()) for fi in f.fields()]

            g = f.geometry()
            # We don't care about M or Z
            g.geometry().dropMValue()
            g.geometry().dropZValue()

            attributes = dict(zip(fields, flddata))
            # We add the _FID_ manually
            fid = int(f.id())
            attributes[self.idField] = fid
            attributes['_calc_length_'] = g.length()

            # Note:  Using layer level geometry type
            if g.wkbType() == QgsWKBTypes.Point:
                self.features[fid] = attributes
                self.G.add_node(g.asPoint())
            elif g.wkbType() in (QgsWKBTypes.LineString, QgsWKBTypes.MultiLineString):
                for edge in self.edges_from_line(g, attributes, simplify, geom_attrs):
                    e1, e2, attr = edge
                    self.features[fid] = attr
                    self.G.add_edge(e1, e2, key=attr[self.idField])
            else:
                raise ImportError("GeometryType {} not supported. For now we only support LineString types.".
                                  format(QgsWKBTypes.displayString(int(g.wkbType()))))



    def edges_from_line(self, geom, attrs, simplify=True, geom_attrs=True):
        """
        This is repurposed from the shape helper here:
        https://github.com/networkx/networkx/blob/master/networkx/readwrite/nx_shp.py
        :return:
        """
        if geom.wkbType() == QgsWKBTypes.LineString:
            pline = geom.asPolyline()
            if simplify:
                edge_attrs = attrs.copy()
                # DEBUGGING
                edge_attrs["Wkt"] = geom.exportToWkt()
                if geom_attrs:
                    edge_attrs["Wkb"] = geom.asWkb()
                    edge_attrs["Wkt"] = geom.exportToWkt()
                    edge_attrs["Json"] = geom.exportToGeoJSON()
                yield (pline[0], pline[-1], edge_attrs)
            else:
                for i in range(0, len(pline) - 1):
                    pt1 = pline[i]
                    pt2 = pline[i + 1]
                    edge_attrs = attrs.copy()
                    if geom_attrs:
                        segment = ogr.Geometry(ogr.wkbLineString)
                        segment.AddPoint_2D(pt1[0], pt1[1])
                        segment.AddPoint_2D(pt2[0], pt2[1])
                        edge_attrs["Wkb"] = segment.asWkb()
                        edge_attrs["Wkt"] = segment.exportToWkt()
                        edge_attrs["Json"] = segment.exportToGeoJSON()
                        del segment
                    yield (pt1, pt2, edge_attrs)

        # TODO: MULTILINESTRING MIGHT NOT WORK
        elif geom.wkbType() == QgsWKBTypes.MultiLineString:
            for i in range(geom.GetGeometryCount()):
                geom_i = geom.GetGeometryRef(i)
                for edge in self.edges_from_line(geom_i, attrs, simplify, geom_attrs):
                    yield edge


    def findEdgewithID(self, id):
        """
        One line helper function to find an edge with a given ID
        because the graph is a multiDiGraph there may be multiple edges for
        each node pair so we need to return an index to which one we mean too
        :param id:
        :return: ((edgetuple), edgeindex, attr)
        """
        # [self.G.get_edge_data(*np) for np in self.G.edges_iter()]
        foundEdge = None
        for np in self.G.edges_iter():
            for k in self.G.get_edge_data(*np).iterkeys():
                if k == id:
                    # EdgeObj = namedtuple('EdgeObj', ['EdgeTuple', 'fid'], verbose=True)
                    foundEdge = EdgeObj(np, k)
                    break

            if foundEdge is not None:
                break

        return foundEdge
        # def anyNodePairs(np, nid):
        #     return next(iter([(np, k, attr) for k, attr in self.G.get_edge_data(*np).iteritems() if attr[self.idField] == nid]), None)
        #
        # return next(iter([anyNodePairs(np, id) for np in self.G.edges_iter()]), None)

    def logInfo(self, msg):
        if self.msgcallback:
            self.msgcallback(msg, color="green")
        self.logmsgs.append(msg)
        self.logger.info(msg)

    def logError(self, msg):
        if self.msgcallback:
            self.msgcallback(msg, color="red")
        self.logmsgs.append("[ERROR]" + msg)
        self.logger.error(msg)