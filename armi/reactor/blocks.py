# Copyright 2019 TerraPower, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Defines blocks, which are axial chunks of assemblies. They contain most of the state variables,
including power, flux, and homogenized number densities.

Assemblies are made of blocks. Blocks are made of components.
"""
from typing import Optional, Type, Tuple, ClassVar
import collections
import copy
import math

import numpy as np

from armi import nuclideBases
from armi import runLog
from armi.bookkeeping import report
from armi.nucDirectory import elements
from armi.nuclearDataIO import xsCollections
from armi.physics.neutronics import GAMMA
from armi.physics.neutronics import NEUTRON
from armi.reactor import blockParameters
from armi.reactor import components
from armi.reactor import composites
from armi.reactor import geometry
from armi.reactor import grids
from armi.reactor import parameters
from armi.reactor.components import basicShapes
from armi.reactor.components.basicShapes import Hexagon, Circle
from armi.reactor.components.complexShapes import Helix
from armi.reactor.flags import Flags
from armi.reactor.parameters import ParamLocation
from armi.utils import densityTools
from armi.utils import hexagon
from armi.utils import units
from armi.utils.plotting import plotBlockFlux
from armi.utils.units import TRACE_NUMBER_DENSITY

PIN_COMPONENTS = [
    Flags.CONTROL,
    Flags.PLENUM,
    Flags.SHIELD,
    Flags.FUEL,
    Flags.CLAD,
    Flags.PIN,
    Flags.WIRE,
]

_PitchDefiningComponent = Optional[Tuple[Type[components.Component], ...]]


class Block(composites.Composite):
    """
    A homogenized axial slab of material.

    Blocks are stacked together to form assemblies.
    """

    uniqID = 0

    # dimension used to determine which component defines the block's pitch
    PITCH_DIMENSION = "op"

    # component type that can be considered a candidate for providing pitch
    PITCH_COMPONENT_TYPE: ClassVar[_PitchDefiningComponent] = None

    pDefs = blockParameters.getBlockParameterDefinitions()

    def __init__(self, name: str, height: float = 1.0):
        """
        Builds a new ARMI block.

        name : str
            The name of this block

        height : float, optional
            The height of the block in cm. Defaults to 1.0 so that ``getVolume`` assumes unit height.
        """
        composites.Composite.__init__(self, name)
        self.p.height = height
        self.p.heightBOL = height

        self.p.orientation = np.array((0.0, 0.0, 0.0))

        self.points = []
        self.macros = None

        # flag to indicated when DerivedShape children must be updated.
        self.derivedMustUpdate = False

        # which component to use to determine block pitch, along with its 'op'
        self._pitchDefiningComponent = (None, 0.0)

        # TODO: what's causing these to have wrong values at BOL?
        for problemParam in ["THcornTemp", "THedgeTemp"]:
            self.p[problemParam] = []

        for problemParam in [
            "residence",
            "bondRemoved",
            "fluence",
            "fastFluence",
            "fastFluencePeak",
            "displacementX",
            "displacementY",
            "fluxAdj",
            "buRate",
            "eqRegion",
            "fissileFraction",
        ]:
            self.p[problemParam] = 0.0

    def __repr__(self):
        # be warned, changing this might break unit tests on input file generations
        return "<{type} {name} at {loc} XS: {xs} BU GP: {bu}>".format(
            type=self.getType(),
            name=self.getName(),
            xs=self.p.xsType,
            bu=self.p.buGroup,
            loc=self.getLocation(),
        )

    def __deepcopy__(self, memo):
        """
        Custom deepcopy behavior to prevent duplication of macros and _lumpedFissionProducts.

        We detach the recursive links to the parent and the reactor to prevent blocks carrying large
        independent copies of stale reactors in memory. If you make a new block, you must add it to
        an assembly and a reactor.
        """
        # add self to memo to prevent child objects from duplicating the parent block
        memo[id(self)] = b = self.__class__.__new__(self.__class__)

        # use __getstate__ and __setstate__ pickle-methods to initialize
        state = self.__getstate__()  # __getstate__ removes parent
        del state["macros"]
        del state["_lumpedFissionProducts"]
        b.__setstate__(copy.deepcopy(state, memo))

        # assign macros and LFP
        b.macros = self.macros
        b._lumpedFissionProducts = self._lumpedFissionProducts

        return b

    def createHomogenizedCopy(self, pinSpatialLocators=False):
        """
        Create a copy of a block.

        Notes
        -----
        Used to implement a copy function for specific block types that can be much faster than a
        deepcopy by glossing over details that may be unnecessary in certain contexts.

        This base class implementation is just a deepcopy of the block, in full detail (not
        homogenized).
        """
        return copy.deepcopy(self)

    @property
    def core(self):
        from armi.reactor.reactors import Core

        c = self.getAncestor(lambda c: isinstance(c, Core))
        return c

    def makeName(self, assemNum, axialIndex):
        """
        Generate a standard block from assembly number.

        This also sets the block-level assembly-num param.

        Once, we used a axial-character suffix to represent the axial index, but this is inherently
        limited so we switched to a numerical name. The axial suffix needs can be brought in to
        plugins that require them.

        Examples
        --------
        >>> makeName(120, 5)
        'B0120-005'
        """
        self.p.assemNum = assemNum
        return "B{0:04d}-{1:03d}".format(assemNum, axialIndex)

    def getSmearDensity(self, cold=True):
        """
        Compute the smear density of pins in this block.

        Smear density is the area of the fuel divided by the area of the space available for fuel
        inside the cladding. Other space filled with solid materials is not considered available. If
        all the area is fuel, it has 100% smear density. Lower smear density allows more room for
        swelling.

        .. warning:: This requires circular fuel and circular cladding. Designs that vary
            from this will be wrong. It may make sense in the future to put this somewhere a
            bit more design specific.

        Notes
        -----
        This only considers circular objects. If you have a cladding that is not a circle, it will
        be ignored.

        Negative areas can exist for void gaps in the fuel pin. A negative area in a gap represents
        overlap area between two solid components. To account for this additional space within the
        pin cladding the abs(negativeArea) is added to the inner cladding area.

        Parameters
        ----------
        cold : bool, optional
            If false, returns the smear density at hot temperatures

        Returns
        -------
        smearDensity : float
            The smear density as a fraction
        """
        fuels = self.getComponents(Flags.FUEL)
        if not fuels:
            return 0.0  # Smear density is not computed for non-fuel blocks

        circles = self.getComponentsOfShape(components.Circle)
        if not circles:
            raise ValueError(
                "Cannot get smear density of {}. There are no circular components.".format(
                    self
                )
            )
        clads = set(self.getComponents(Flags.CLAD)).intersection(set(circles))
        if not clads:
            raise ValueError(
                "Cannot get smear density of {}. There are no clad components.".format(
                    self
                )
            )

        # Compute component areas
        cladID = np.mean([clad.getDimension("id", cold=cold) for clad in clads])
        innerCladdingArea = (
            math.pi * (cladID**2) / 4.0 * self.getNumComponents(Flags.FUEL)
        )
        fuelComponentArea = 0.0
        unmovableComponentArea = 0.0
        negativeArea = 0.0
        for c in self.getSortedComponentsInsideOfComponent(clads.pop()):
            componentArea = c.getArea(cold=cold)
            if c.isFuel():
                fuelComponentArea += componentArea
            elif c.hasFlags(Flags.SLUG):
                # this flag designates that this clad/slug combination isn't fuel and shouldn't be
                # counted in the average
                pass
            else:
                if c.containsSolidMaterial():
                    unmovableComponentArea += componentArea
                elif c.containsVoidMaterial() and componentArea < 0.0:
                    if cold:  # will error out soon
                        runLog.error(
                            "{} with id {} and od {} has negative area at cold dimensions".format(
                                c,
                                c.getDimension("id", cold=True),
                                c.getDimension("od", cold=True),
                            )
                        )
                    negativeArea += abs(componentArea)
        if cold and negativeArea:
            raise ValueError(
                "Negative component areas exist on {}. Check that the cold dimensions are properly aligned "
                "and no components overlap.".format(self)
            )
        innerCladdingArea += negativeArea  # See note 2
        totalMovableArea = innerCladdingArea - unmovableComponentArea
        smearDensity = fuelComponentArea / totalMovableArea

        return smearDensity

    def autoCreateSpatialGrids(self):
        """
        Creates a spatialGrid for a Block.

        Blocks do not always have a spatialGrid from Blueprints, but, some Blocks can have their
        spatialGrids inferred based on the multiplicty of their components.
        This would add the ability to create a spatialGrid for a Block and give its children
        the corresponding spatialLocators if certain conditions are met.

        Raises
        ------
        ValueError
            If the multiplicities of the block are not only 1 or N or if generated ringNumber leads
            to more positions than necessary.
        """
        raise NotImplementedError()

    def getMgFlux(self, adjoint=False, average=False, volume=None, gamma=False):
        """
        Returns the multigroup neutron flux in [n/cm^2/s].

        The first entry is the first energy group (fastest neutrons). Each additional
        group is the next energy group, as set in the ISOTXS library.

        It is stored integrated over volume on self.p.mgFlux

        Parameters
        ----------
        adjoint : bool, optional
            Return adjoint flux instead of real

        average : bool, optional
            If true, will return average flux between latest and previous. Doesn't work
            for pin detailed yet

        volume: float, optional
            If average=True, the volume-integrated flux is divided by volume before being returned.
            The user may specify a volume, or the function will obtain the block volume directly.

        gamma : bool, optional
            Whether to return the neutron flux or the gamma flux.

        Returns
        -------
        flux : multigroup neutron flux in [n/cm^2/s]
        """
        flux = composites.ArmiObject.getMgFlux(
            self, adjoint=adjoint, average=False, volume=volume, gamma=gamma
        )
        if average and np.any(self.p.lastMgFlux):
            volume = volume or self.getVolume()
            lastFlux = self.p.lastMgFlux / volume
            flux = (flux + lastFlux) / 2.0
        return flux

    def setPinMgFluxes(self, fluxes, adjoint=False, gamma=False):
        """
        Store the pin-detailed multi-group neutron flux.

        The [g][i] indexing is transposed to be a list of lists, one for each pin. This makes it
        simple to do depletion for each pin, etc.

        Parameters
        ----------
        fluxes : 2-D list of floats
            The block-level pin multigroup fluxes. fluxes[g][i] represents the flux in group g for
            pin i. Flux units are the standard n/cm^2/s.
            The "ARMI pin ordering" is used, which is counter-clockwise from 3 o'clock.
        adjoint : bool, optional
            Whether to set real or adjoint data.
        gamma : bool, optional
            Whether to set gamma or neutron data.

        Outputs
        -------
        self.p.pinMgFluxes : 2-D array of floats
            The block-level pin multigroup fluxes. pinMgFluxes[g][i] represents the flux in group g
            for pin i. Flux units are the standard n/cm^2/s.
            The "ARMI pin ordering" is used, which is counter-clockwise from 3 o'clock.
        """
        pinFluxes = []

        G, nPins = fluxes.shape

        for pinNum in range(1, nPins + 1):
            thisPinFlux = []

            if self.hasFlags(Flags.FUEL):
                pinLoc = self.p.pinLocation[pinNum - 1]
            else:
                pinLoc = pinNum

            for g in range(G):
                thisPinFlux.append(fluxes[g][pinLoc - 1])
            pinFluxes.append(thisPinFlux)

        pinFluxes = np.array(pinFluxes)
        if gamma:
            if adjoint:
                raise ValueError("Adjoint gamma flux is currently unsupported.")
            else:
                self.p.pinMgFluxesGamma = pinFluxes
        else:
            if adjoint:
                self.p.pinMgFluxesAdj = pinFluxes
            else:
                self.p.pinMgFluxes = pinFluxes

    def getMicroSuffix(self):
        """
        Returns the microscopic library suffix (e.g. 'AB') for this block.

        DIF3D and MC2 are limited to 6 character nuclide labels. ARMI by convention uses
        the first 4 for nuclide name (e.g. U235, PU39, etc.) and then uses the 5th
        character for cross-section type and the 6th for burnup group. This allows a
        variety of XS sets to be built modeling substantially different blocks.

        Notes
        -----
        The single-letter use for xsType and buGroup limit users to 26 groups of each.
        ARMI will allow 2-letter xsType designations if and only if the `buGroups`
        setting has length 1 (i.e. no burnup groups are defined). This is useful for
        high-fidelity XS modeling of V&V models such as the ZPPRs.
        """
        bu = self.p.buGroup
        if not bu:
            raise RuntimeError(
                "Cannot get MicroXS suffix because {0} in {1} does not have a burnup group"
                "".format(self, self.parent)
            )

        xsType = self.p.xsType
        if len(xsType) == 1:
            return xsType + bu
        elif len(xsType) == 2 and ord(bu) > ord("A"):
            raise ValueError(
                "Use of multiple burnup groups is not allowed with multi-character xs groups!"
            )
        else:
            return xsType

    def getHeight(self):
        """Return the block height."""
        return self.p.height

    def setHeight(self, modifiedHeight, conserveMass=False, adjustList=None):
        """
        Set a new height of the block.

        Parameters
        ----------
        modifiedHeight : float
            The height of the block in cm

        conserveMass : bool, optional
            Conserve mass of nuclides in ``adjustList``.

        adjustList : list, optional
            Nuclides that will be conserved in conserving mass in the block. It is recommended to pass a list of
            all nuclides in the block.

        Notes
        -----
        There is a coupling between block heights, the parent assembly axial mesh,
        and the ztop/zbottom/z params of the sibling blocks. When you set a height,
        all those things are invalidated. Thus, this method has to go through and
        update them via ``parent.calculateZCoords``. This could be inefficient
        though it has not been identified as a bottleneck. Possible improvements
        include deriving z/ztop/zbottom on the fly and invalidating the parent mesh
        with some kind of flag, signaling it to recompute itself on demand.
        Developers can get around some of the O(N^2) scaling of this by setting
        ``p.height`` directly but they must know to update the dependent objects
        after they do that. Use with care.

        See Also
        --------
        armi.reactor.reactors.Core.updateAxialMesh
            May need to be called after this.
        armi.reactor.assemblies.Assembly.calculateZCoords
            Recalculates z-coords, automatically called by this.
        """
        originalHeight = self.getHeight()  # get before modifying
        if modifiedHeight < 0.0:
            raise ValueError(
                "Cannot set height of block {} to height of {} cm".format(
                    self, modifiedHeight
                )
            )
        self.p.height = modifiedHeight
        self.clearCache()
        if conserveMass:
            if originalHeight != modifiedHeight:
                if not adjustList:
                    raise ValueError(
                        "Nuclides in ``adjustList`` must be provided to conserve mass."
                    )
                self.adjustDensity(originalHeight / modifiedHeight, adjustList)
        if self.parent:
            self.parent.calculateZCoords()

    def getWettedPerimeter(self):
        raise NotImplementedError

    def getFlowAreaPerPin(self):
        """
        Return the flowing coolant area of the block in cm^2, normalized to the number of pins in the block.

        NumPins looks for max number of fuel, clad, control, etc.

        See Also
        --------
        armi.reactor.blocks.Block.getNumPins
            figures out numPins
        """
        numPins = self.getNumPins()
        try:
            return self.getComponent(Flags.COOLANT, exact=True).getArea() / numPins
        except ZeroDivisionError:
            raise ZeroDivisionError(
                "Block {} has 0 pins (fuel, clad, control, shield, etc.). Thus, its flow area "
                "per pin is undefined.".format(self)
            )

    def getHydraulicDiameter(self):
        raise NotImplementedError

    def adjustUEnrich(self, newEnrich):
        """
        Adjust U-235/U-238 mass ratio to a mass enrichment.

        Parameters
        ----------
        newEnrich : float
            New U-235 enrichment in mass fraction

        Notes
        -----
        completeInitialLoading must be run because adjusting the enrichment actually
        changes the mass slightly and you can get negative burnups, which you do not want.
        """
        fuels = self.getChildrenWithFlags(Flags.FUEL)

        if fuels:
            for fuel in fuels:
                fuel.adjustMassEnrichment(newEnrich)
        else:
            # no fuel in this block
            tU = self.getNumberDensity("U235") + self.getNumberDensity("U238")
            if tU:
                self.setNumberDensity("U235", tU * newEnrich)
                self.setNumberDensity("U238", tU * (1.0 - newEnrich))

        self.completeInitialLoading()

    def getLocation(self):
        """Return a string representation of the location.

        .. impl:: Location of a block is retrievable.
            :id: I_ARMI_BLOCK_POSI0
            :implements: R_ARMI_BLOCK_POSI

            If the block does not have its ``core`` attribute set, if the block's
            parent does not have a ``spatialGrid`` attribute, or if the block
            does not have its location defined by its ``spatialLocator`` attribute,
            return a string indicating that it is outside of the core.

            Otherwise, use the :py:class:`~armi.reactor.grids.Grid.getLabel` static
            method to convert the block's indices into a string like "XXX-YYY-ZZZ".
            For hexagonal geometry, "XXX" is the zero-padded hexagonal core ring,
            "YYY" is the zero-padded position in that ring, and "ZZZ" is the zero-padded
            block axial index from the bottom of the core.
        """
        if self.core and self.parent.spatialGrid and self.spatialLocator:
            return self.core.spatialGrid.getLabel(
                self.spatialLocator.getCompleteIndices()
            )
        else:
            return "ExCore"

    def coords(self):
        """
        Returns the coordinates of the block.

        .. impl:: Coordinates of a block are queryable.
            :id: I_ARMI_BLOCK_POSI1
            :implements: R_ARMI_BLOCK_POSI

            Calls to the :py:meth:`~armi.reactor.grids.locations.IndexLocation.getGlobalCoordinates`
            method of the block's ``spatialLocator`` attribute, which recursively
            calls itself on all parents of the block to get the coordinates of the
            block's centroid in 3D cartesian space.
        """
        return self.spatialLocator.getGlobalCoordinates()

    def setBuLimitInfo(self):
        """Sets burnup limit based on igniter, feed, etc."""
        if self.p.buRate == 0:
            # might be cycle 1 or a non-burning block
            self.p.timeToLimit = 0.0
        else:
            timeLimit = (
                self.p.buLimit - self.p.percentBu
            ) / self.p.buRate + self.p.residence
            self.p.timeToLimit = (timeLimit - self.p.residence) / units.DAYS_PER_YEAR

    def getMaxArea(self):
        raise NotImplementedError

    def getArea(self, cold=False):
        """
        Return the area of a block for a full core or a 1/3 core model.

        Area is consistent with the area in the model, so if you have a central
        assembly in a 1/3 symmetric model, this will return 1/3 of the total
        area of the physical assembly. This way, if you take the sum
        of the areas in the core (or count the atoms in the core, etc.),
        you will have the proper number after multiplying by the model symmetry.

        Parameters
        ----------
        cold : bool
            flag to indicate that cold (as input) dimensions are required

        Notes
        -----
        This might not work for a 1/6 core model (due to symmetry line issues).

        Returns
        -------
        area : float (cm^2)

        See Also
        --------
        armi.reactor.blocks.Block.getMaxArea
            return the full area of the physical assembly disregarding model symmetry
        """
        # this caching requires that you clear the cache every time you adjust anything
        # including temperature and dimensions.
        area = self._getCached("area")
        if area:
            return area

        a = 0.0
        for c in self.getChildren():
            myArea = c.getArea(cold=cold)
            a += myArea
        fullArea = a

        # correct the fullHexArea by the symmetry factor
        # this factor determines if the hex has been clipped by symmetry lines
        area = fullArea / self.getSymmetryFactor()

        self._setCache("area", area)
        return area

    def getVolume(self):
        """
        Return the volume of a block.

        .. impl:: Volume of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS0
            :implements: R_ARMI_BLOCK_DIMS

            Loops over all the components in the block, calling
            :py:meth:`~armi.reactor.components.component.Component.getVolume` on
            each and summing the result. The summed value is then divided by
            the symmetry factor of the block to account for reduced volumes of
            blocks in certain symmetric representations.

        Returns
        -------
        volume : float
            Block or component volume in cm^3
        """
        # use symmetryFactor in case the assembly is sitting on a boundary and needs to be cut in half, etc.
        vol = sum(c.getVolume() for c in self)
        return vol / self.getSymmetryFactor()

    def getSymmetryFactor(self):
        """
        Return a scaling factor due to symmetry on the area of the block or its components.

        Takes into account assemblies that are bisected or trisected by symmetry lines

        In 1/3 symmetric cases, the central assembly is 1/3 a full area.
        If edge assemblies are included in a model, the symmetry factor along
        both edges for overhanging assemblies should be 2.0. However,
        ARMI runs in most scenarios with those assemblies on the 120-edge removed,
        so the symmetry factor should generally be just 1.0.

        See Also
        --------
        armi.reactor.converters.geometryConverter.EdgeAssemblyChanger.scaleParamsRelatedToSymmetry
        """
        return 1.0

    def adjustDensity(self, frac, adjustList, returnMass=False):
        """
        adjusts the total density of each nuclide in adjustList by frac.

        Parameters
        ----------
        frac : float
            The fraction of the current density that will remain after this operation

        adjustList : list
            List of nuclide names that will be adjusted.

        returnMass : bool
            If true, will return mass difference.

        Returns
        -------
        mass : float
            Mass difference in grams. If you subtract mass, mass will be negative.
            If returnMass is False (default), this will always be zero.
        """
        self._updateDetailedNdens(frac, adjustList)

        mass = 0.0
        if returnMass:
            # do this with a flag to enable faster operation when mass is not needed.
            volume = self.getVolume()

        numDensities = self.getNuclideNumberDensities(adjustList)

        for nuclideName, dens in zip(adjustList, numDensities):
            if not dens:
                # don't modify zeros.
                continue
            newDens = dens * frac
            # add a little so components remember
            self.setNumberDensity(nuclideName, newDens + TRACE_NUMBER_DENSITY)
            if returnMass:
                mass += densityTools.getMassInGrams(nuclideName, volume, newDens - dens)

        return mass

    def _updateDetailedNdens(self, frac, adjustList):
        """
        Update detailed number density which is used by hi-fi depleters such as ORIGEN.

        Notes
        -----
        This will perturb all number densities so it is assumed that if one of the active densities
        is perturbed, all of htem are perturbed.
        """
        if self.p.detailedNDens is None:
            # BOL assems get expanded to a reference so the first check is needed so it
            # won't call .blueprints on None since BOL assems don't have a core/r
            return
        if any(nuc in self.core.r.blueprints.activeNuclides for nuc in adjustList):
            self.p.detailedNDens *= frac
            # Other power densities do not need to be updated as they are calculated in
            # the global flux interface, which occurs after axial expansion from crucible
            # on the interface stack.
            self.p.pdensDecay *= frac

    def completeInitialLoading(self, bolBlock=None):
        """
        Does some BOL bookkeeping to track things like BOL HM density for burnup tracking.

        This should run after this block is loaded up at BOC (called from
        Reactor.initialLoading).

        The original purpose of this was to get the moles HM at BOC for the moles
        Pu/moles HM at BOL calculation.

        This also must be called after modifying something like the smear density or zr
        fraction in an optimization case. In ECPT cases, a BOL block must be passed or
        else the burnup will try to get based on a pre-burned value.

        Parameters
        ----------
        bolBlock : Block, optional
            A BOL-state block of this block type, required for perturbed equilibrium cases.
            Must have the same enrichment as this block!

        Returns
        -------
        hmDens : float
            The heavy metal number density of this block.

        See Also
        --------
        Reactor.importGeom
        depletion._updateBlockParametersAfterDepletion
        """
        if bolBlock is None:
            bolBlock = self

        hmDens = bolBlock.getHMDens()  # total homogenized heavy metal number density
        self.p.nHMAtBOL = hmDens
        self.p.molesHmBOL = self.getHMMoles()
        self.p.puFrac = (
            self.getPuMoles() / self.p.molesHmBOL if self.p.molesHmBOL > 0.0 else 0.0
        )

        try:
            # non-pinned reactors (or ones without cladding) will not use smear density
            self.p.smearDensity = self.getSmearDensity()
        except ValueError:
            pass

        self.p.enrichmentBOL = self.getFissileMassEnrich()
        massHmBOL = 0.0
        sf = self.getSymmetryFactor()
        for child in self:
            hmMass = child.getHMMass() * sf
            massHmBOL += hmMass
            # Components have a massHmBOL parameter but not every composite will
            if isinstance(child, components.Component):
                child.p.massHmBOL = hmMass

        self.p.massHmBOL = massHmBOL

        return hmDens

    def setB10VolParam(self, heightHot):
        """
        Set the b.p.initialB10ComponentVol param according to the volume of boron-10 containing components.

        Parameters
        ----------
        heightHot : Boolean
            True if self.height() is cold height
        """
        # exclude fuel components since they could have slight B10 impurity and
        # this metric is not relevant for fuel.
        b10Comps = [c for c in self if c.getNumberDensity("B10") and not c.isFuel()]
        if not b10Comps:
            return

        # get the highest density comp dont want to sum all because some
        # comps might have very small impurities of boron and adding this
        # volume wont be conservative for captures per cc.
        b10Comp = sorted(b10Comps, key=lambda x: x.getNumberDensity("B10"))[-1]

        if len(b10Comps) > 1:
            runLog.warning(
                f"More than one boron10-containing component found  in {self.name}. "
                f"Only {b10Comp} will be considered for calculation of initialB10ComponentVol "
                "Since adding multiple volumes is not conservative for captures/cc."
                f"All compos found {b10Comps}",
                single=True,
            )
        if self.isFuel():
            runLog.warning(
                f"{self.name} has both fuel and initial b10. "
                "b10 volume may not be conserved with axial expansion.",
                single=True,
            )

        # calc volume of boron components
        coldArea = b10Comp.getArea(cold=True)
        coldFactor = b10Comp.getThermalExpansionFactor() if heightHot else 1
        coldHeight = self.getHeight() / coldFactor
        self.p.initialB10ComponentVol = coldArea * coldHeight

    def replaceBlockWithBlock(self, bReplacement):
        """
        Replace the current block with the replacementBlock.

        Typically used in the insertion of control rods.
        """
        paramsToSkip = set(
            self.p.paramDefs.inCategory(parameters.Category.retainOnReplacement).names
        )

        tempBlock = copy.deepcopy(bReplacement)
        oldParams = self.p
        newParams = self.p = tempBlock.p
        for paramName in paramsToSkip:
            newParams[paramName] = oldParams[paramName]

        # update synchronization information
        self.p.assigned = parameters.SINCE_ANYTHING
        paramDefs = self.p.paramDefs
        for paramName in set(newParams.keys()) - paramsToSkip:
            paramDefs[paramName].assigned = parameters.SINCE_ANYTHING

        newComponents = tempBlock.getChildren()
        self.setChildren(newComponents)
        self.clearCache()

    @staticmethod
    def plotFlux(core, fName=None, bList=None, peak=False, adjoint=False, bList2=[]):
        # Block.plotFlux has been moved to utils.plotting as plotBlockFlux, which is a
        # better fit.
        # We don't want to remove the plotFlux function in the Block namespace yet
        # in case client code is depending on this function existing here. This is just
        # a simple pass-through function that passes the arguments along to the actual
        # implementation in its new location.
        plotBlockFlux(core, fName, bList, peak, adjoint, bList2)

    def _updatePitchComponent(self, c):
        """
        Update the component that defines the pitch.

        Given a Component, compare it to the current component that defines the pitch of the Block.
        If bigger, replace it.
        We need different implementations of this to support different logic for determining the
        form of pitch and the concept of "larger".

        See Also
        --------
        CartesianBlock._updatePitchComponent
        """
        # Some block types don't have a clearly defined pitch (e.g. ThRZ)
        if self.PITCH_COMPONENT_TYPE is None:
            return

        if not isinstance(c, self.PITCH_COMPONENT_TYPE):
            return

        try:
            componentPitch = c.getDimension(self.PITCH_DIMENSION)
        except parameters.UnknownParameterError:
            # some components dont have the appropriate parameter
            return

        if componentPitch and (componentPitch > self._pitchDefiningComponent[1]):
            self._pitchDefiningComponent = (c, componentPitch)

    def add(self, c):
        composites.Composite.add(self, c)

        self.derivedMustUpdate = True
        self.clearCache()
        try:
            mult = int(c.getDimension("mult"))
            if self.p.percentBuByPin is None or len(self.p.percentBuByPin) < mult:
                # this may be a little wasteful, but we can fix it later...
                self.p.percentBuByPin = [0.0] * mult
        except AttributeError:
            # maybe adding a Composite of components rather than a single
            pass
        self._updatePitchComponent(c)

    def removeAll(self, recomputeAreaFractions=True):
        for c in self.getChildren():
            self.remove(c, recomputeAreaFractions=False)
        if recomputeAreaFractions:  # only do this once
            self.getVolumeFractions()

    def remove(self, c, recomputeAreaFractions=True):
        composites.Composite.remove(self, c)
        self.clearCache()

        if c is self._pitchDefiningComponent[0]:
            self._pitchDefiningComponent = (None, 0.0)
            pc = self.getLargestComponent(self.PITCH_DIMENSION)
            if pc is not None:
                self._updatePitchComponent(pc)

        if recomputeAreaFractions:
            self.getVolumeFractions()

    def getComponentsThatAreLinkedTo(self, comp, dim):
        """
        Determine which dimensions of which components are linked to a specific dimension of a particular component.

        Useful for breaking fuel components up into individuals and making sure
        anything that was linked to the fuel mult (like the cladding mult) stays correct.

        Parameters
        ----------
        comp : Component
            The component that the results are linked to
        dim : str
            The name of the dimension that the results are linked to

        Returns
        -------
        linkedComps : list
            A list of (components,dimName) that are linked to this component, dim.
        """
        linked = []
        for c in self.iterComponents():
            for dimName, val in c.p.items():
                if c.dimensionIsLinked(dimName):
                    requiredComponent = val[0]
                    if requiredComponent is comp and val[1] == dim:
                        linked.append((c, dimName))
        return linked

    def getComponentsInLinkedOrder(self, componentList=None):
        """
        Return a list of the components in order of their linked-dimension dependencies.

        Parameters
        ----------
        components : list, optional
            A list of components to consider. If None, this block's components will be used.

        Notes
        -----
        This means that components other components are linked to come first.
        """
        if componentList is None:
            componentList = self.getComponents()
        cList = collections.deque(componentList)
        orderedComponents = []
        # Loop through the components until there are none left.
        counter = 0
        while cList:
            candidate = cList.popleft()  # take first item in list
            cleared = True  # innocent until proven guilty
            # loop through all dimensions in this component to determine its dependencies
            for dimName, val in candidate.p.items():
                if candidate.dimensionIsLinked(dimName):
                    # In linked dimensions, val = (component, dimName)
                    requiredComponent = val[0]
                    if requiredComponent not in orderedComponents:
                        # this component depends on one that is not in the ordered list yet.
                        # do not add it.
                        cleared = False
                        break  # short circuit. One failed lookup is enough to flag this component as dirty.
            if cleared:
                # this candidate is free of dependencies and is ready to be added.
                orderedComponents.append(candidate)
            else:
                cList.append(candidate)

            counter += 1
            if counter > 1000:
                cList.append(candidate)
                runLog.error(
                    "The component {0} in {1} contains a dimension that is linked to another component, "
                    " but the required component is not present in the block. They may also be other dependency fails. "
                    "The component dims are {2}".format(cList[0], self, cList[0].p)
                )
                raise RuntimeError("Cannot locate linked component.")
        return orderedComponents

    def getSortedComponentsInsideOfComponent(self, component):
        """
        Returns a list of components inside of the given component sorted from innermost to outermost.

        Parameters
        ----------
        component : object
            Component to look inside of.

        Notes
        -----
        If you just want sorted components in this block, use ``sorted(self)``.
        This will never include any ``DerivedShape`` objects. Since they have a derived
        area they don't have a well-defined dimension. For now we just ignore them.
        If they are desired in the future some knowledge of their dimension will be
        required while they are being derived.
        """
        sortedComponents = sorted(self)
        componentIndex = sortedComponents.index(component)
        sortedComponents = sortedComponents[:componentIndex]
        return sortedComponents

    def getNumPins(self):
        """Return the number of pins in this block.

        .. impl:: Get the number of pins in a block.
            :id: I_ARMI_BLOCK_NPINS
            :implements: R_ARMI_BLOCK_NPINS

            Uses some simple criteria to infer the number of pins in the block.

            For every flag in the module list :py:data:`~armi.reactor.blocks.PIN_COMPONENTS`,
            loop over all components of that type in the block. If the component
            is an instance of :py:class:`~armi.reactor.components.basicShapes.Circle`,
            add its multiplicity to a list, and sum that list over all components
            with each given flag.

            After looping over all possibilities, return the maximum value returned
            from the process above, or if no compatible components were found,
            return zero.
        """
        nPins = [
            sum(
                [
                    (
                        int(c.getDimension("mult"))
                        if isinstance(c, basicShapes.Circle)
                        else 0
                    )
                    for c in self.iterComponents(compType)
                ]
            )
            for compType in PIN_COMPONENTS
        ]
        return 0 if not nPins else max(nPins)

    def mergeWithBlock(self, otherBlock, fraction):
        """
        Turns this block into a mixture of this block and some other block.

        Parameters
        ----------
        otherBlock : Block
            The block to mix this block with. The other block will not be modified.

        fraction : float
            Fraction of the other block to mix in with this block. If 0.1 is passed in, this block
            will become 90% what it originally was and 10% what the other block is.

        Notes
        -----
        This merges on a high level (using number densities). Components will not be merged.

        This is used e.g. for inserting a control block partially to get a very tight criticality
        control.  In this case, a control block would be merged with a duct block. It is also used
        when a control rod is specified as a certain length but that length does not fit exactly
        into a full block.
        """
        numDensities = self.getNumberDensities()
        otherBlockDensities = otherBlock.getNumberDensities()
        newDensities = {}

        # Make sure to hit all nuclides in union of blocks
        for nucName in set(numDensities.keys()).union(otherBlockDensities.keys()):
            newDensities[nucName] = (1.0 - fraction) * numDensities.get(
                nucName, 0.0
            ) + fraction * otherBlockDensities.get(nucName, 0.0)

        self.setNumberDensities(newDensities)

    def getComponentAreaFrac(self, typeSpec):
        """
        Returns the area fraction of the specified component(s) among all components in the block.

        Parameters
        ----------
        typeSpec : Flags or list of Flags
            Component types to look up

        Examples
        --------
        >>> b.getComponentAreaFrac(Flags.CLAD)
        0.15

        Returns
        -------
        float
            The area fraction of the component.
        """
        tFrac = sum(f for (c, f) in self.getVolumeFractions() if c.hasFlags(typeSpec))

        if tFrac:
            return tFrac
        else:
            runLog.warning(
                "No component {0} exists on {1}, so area fraction is zero.".format(
                    typeSpec, self
                ),
                single=True,
                label="{0} areaFrac is zero".format(typeSpec),
            )
            return 0.0

    def verifyBlockDims(self):
        """Optional dimension checking."""
        return

    def getDim(self, typeSpec, dimName):
        """
        Search through blocks in this assembly and find the first component of compName.
        Then, look on that component for dimName.

        Parameters
        ----------
        typeSpec : Flags or list of Flags
            Component name, e.g. Flags.FUEL, Flags.CLAD, Flags.COOLANT, ...
        dimName : str
            Dimension name, e.g. 'od', ...

        Returns
        -------
        dimVal : float
            The dimension in cm.

        Examples
        --------
        >>> getDim(Flags.WIRE,'od')
        0.01
        """
        for c in self:
            if c.hasFlags(typeSpec):
                return c.getDimension(dimName.lower())

        raise ValueError(
            "Cannot get Dimension because Flag not found: {0}".format(typeSpec)
        )

    def getPinCenterFlatToFlat(self, cold=False):
        """Return the flat-to-flat distance between the centers of opposing pins in the outermost ring."""
        raise NotImplementedError  # no geometry can be assumed

    def getWireWrapCladGap(self, cold=False):
        """Return the gap betwen the wire wrap and the clad."""
        clad = self.getComponent(Flags.CLAD)
        wire = self.getComponent(Flags.WIRE)
        wireOuterRadius = wire.getBoundingCircleOuterDiameter(cold=cold) / 2.0
        wireInnerRadius = wireOuterRadius - wire.getDimension("od", cold=cold)
        cladOuterRadius = clad.getDimension("od", cold=cold) / 2.0
        return wireInnerRadius - cladOuterRadius

    def getPlenumPin(self):
        """Return the plenum pin if it exists."""
        for c in self.iterComponents(Flags.GAP):
            if self.isPlenumPin(c):
                return c
        return None

    def isPlenumPin(self, c):
        """Return True if the specified component is a plenum pin."""
        # This assumes that anything with the GAP flag will have a valid 'id' dimension. If that
        # were not the case, then we would need to protect the call to getDimension with a
        # try/except
        cIsCenterGapGap = (
            isinstance(c, components.Component)
            and c.hasFlags(Flags.GAP)
            and c.getDimension("id") == 0
        )
        return self.hasFlags([Flags.PLENUM, Flags.ACLP]) and cIsCenterGapGap

    def getPitch(self, returnComp=False):
        """
        Return the center-to-center hex pitch of this block.

        .. impl:: Pitch of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS1
            :implements: R_ARMI_BLOCK_DIMS

            Uses the block's ``_pitchDefiningComponent`` to identify the component
            in the block that defines the pitch. Then uses the
            :py:meth:`~armi.reactor.components.component.Component.getPitchData`
            method of that component to return the pitch for the block, accounting
            for the component's current temperature.

            The ``_pitchDefiningComponent`` attribute can be set by
            :py:meth:`~armi.reactor.blocks.Block.setPitch`, but is typically
            set via a calls to :py:meth:`~armi.reactor.blocks.Block._updatePitchComponent`
            as components are added to the block with :py:meth:`~armi.reactor.blocks.Block.add`.

        Parameters
        ----------
        returnComp : bool, optional
            If true, will return the component that has the maximum pitch as well

        Returns
        -------
        pitch : float or None
            Hex pitch in cm, if well-defined. If there is no clear component for determining pitch,
            returns None
        component : Component or None
            Component that has the max pitch, if returnComp == True. If no component is found to
            define the pitch, returns None

        Notes
        -----
        The block stores a reference to the component that defines the pitch, making the assumption
        that while the dimensions can change, the component containing the largest dimension will
        not. This lets us skip the search for largest component. We still need to ask the largest
        component for its current dimension in case its temperature changed, or was otherwise
        modified.

        See Also
        --------
        setPitch : sets pitch
        """
        c, _p = self._pitchDefiningComponent
        if c is None:
            raise ValueError("{} has no valid pitch defining component".format(self))

        # ask component for dimensions, since they could have changed,
        # due to temperature, for example.
        p = c.getPitchData()
        return (p, c) if returnComp else p

    def hasPinPitch(self):
        """Return True if the block has enough information to calculate pin pitch."""
        return self.spatialGrid is not None

    def getPinPitch(self, cold=False):
        """
        Return sub-block pitch in blocks.

        This assumes the spatial grid is defined by unit steps
        """
        return self.spatialGrid.pitch

    def getDimensions(self, dimension):
        """Return dimensional values of the specified dimension."""
        dimVals = set()
        for c in self.getChildren():
            try:
                dimVal = c.getDimension(dimension)
            except parameters.ParameterError:
                continue
            if dimVal is not None:
                dimVals.add(dimVal)
        return dimVals

    def getLargestComponent(self, dimension):
        """
        Find the component with the largest dimension of the specified type.

        Parameters
        ----------
        dimension: str
            The name of the dimension to find the largest component of.

        Returns
        -------
        largestComponent: armi.reactor.components.Component
            The component with the largest dimension of the specified type.
        """
        maxDim = -float("inf")
        largestComponent = None
        for c in self:
            try:
                dimVal = c.getDimension(dimension)
            except parameters.ParameterError:
                continue
            if dimVal is not None and dimVal > maxDim:
                maxDim = dimVal
                largestComponent = c
        return largestComponent

    def setPitch(self, val, updateBolParams=False):
        """
        Sets outer pitch to some new value.

        This sets the settingPitch and actually sets the dimension of the outer hexagon.

        During a load (importGeom), the setDimension doesn't usually do anything except
        set the setting See Issue 034

        But during a actual case modification (e.g. in an optimization sweep, then the dimension
        has to be set as well.

        See Also
        --------
        getPitch : gets the pitch

        """
        c, _p = self._pitchDefiningComponent
        if c:
            c.setDimension("op", val)
            self._pitchDefiningComponent = (c, val)
        else:
            raise RuntimeError("No pitch-defining component on block {}".format(self))

        if updateBolParams:
            self.completeInitialLoading()

    def getMfp(self, gamma=False):
        r"""
        Calculate the mean free path for neutron or gammas in this block.

        .. math::

            <\Sigma> = \frac{\sum_E(\phi_e \Sigma_e dE)}{\sum_E (\phi_e dE)}  =
            \frac{\sum_E(\phi_e N \sum_{\text{type}}(\sigma_e)  dE}{\sum_E (\phi_e dE))}

        Block macro is the sum of macros of all nuclides.

        phi_g = flux*dE already in multigroup method.

        Returns
        -------
        mfp, mfpAbs, diffusionLength : tuple(float, float float)
        """
        lib = self.core.lib
        flux = self.getMgFlux(gamma=gamma)
        flux = [fi / max(flux) for fi in flux]
        mfpNumerator = np.zeros(len(flux))
        absMfpNumerator = np.zeros(len(flux))
        transportNumerator = np.zeros(len(flux))

        numDensities = self.getNumberDensities()

        # vol = self.getVolume()
        for nucName, nDen in numDensities.items():
            nucMc = nuclideBases.byName[nucName].label + self.getMicroSuffix()
            if gamma:
                micros = lib[nucMc].gammaXS
            else:
                micros = lib[nucMc].micros
            total = micros.total[:, 0]  # 0th order
            transport = micros.transport[:, 0]  # 0th order, [bn]
            absorb = sum(micros.getAbsorptionXS())
            mfpNumerator += nDen * total  # [cm]
            absMfpNumerator += nDen * absorb
            transportNumerator += nDen * transport
        denom = sum(flux)
        mfp = 1.0 / (sum(mfpNumerator * flux) / denom)
        sigmaA = sum(absMfpNumerator * flux) / denom
        sigmaTr = sum(transportNumerator * flux) / denom
        diffusionCoeff = 1 / (3.0 * sigmaTr)
        mfpAbs = 1 / sigmaA
        diffusionLength = math.sqrt(diffusionCoeff / sigmaA)
        return mfp, mfpAbs, diffusionLength

    def setAreaFractionsReport(self):
        for c, frac in self.getVolumeFractions():
            report.setData(
                c.getName(),
                ["{0:10f}".format(c.getArea()), "{0:10f}".format(frac)],
                report.BLOCK_AREA_FRACS,
            )

        # return the group the information went to
        return report.ALL[report.BLOCK_AREA_FRACS]

    def getBlocks(self):
        """
        This method returns all the block(s) included in this block
        its implemented so that methods could iterate over reactors, assemblies
        or single blocks without checking to see what the type of the
        reactor-family object is.
        """
        return [self]

    def updateComponentDims(self):
        """
        This method updates all the dimensions of the components.

        Notes
        -----
        This is VERY useful for defining a ThRZ core out of
        differentialRadialSegements whose dimensions are connected together
        some of these dimensions are derivative and can be updated by changing
        dimensions in a Parameter Component or other linked components

        See Also
        --------
        armi.reactor.components.DifferentialRadialSegment.updateDims
        armi.reactor.components.Parameters
        armi.physics.optimize.OptimizationInterface.modifyCase (look up 'ThRZReflectorThickness')
        """
        for c in self.getComponentsInLinkedOrder():
            try:
                c.updateDims()
            except NotImplementedError:
                runLog.warning("{0} has no updatedDims method -- skipping".format(c))

    def breakFuelComponentsIntoIndividuals(self):
        """
        Split block-level components (in fuel blocks) into pin-level components.

        The fuel component will be broken up according to its multiplicity.

        Order matters! The first pin component will be located at a particular (x, y), which
        will be used in the fluxRecon module to determine the interpolated flux.

        The fuel will become fuel001 through fuel169 if there are 169 pins.
        """
        fuels = self.getChildrenWithFlags(Flags.FUEL)
        if len(fuels) != 1:
            runLog.error(
                "This block contains {0} fuel components: {1}".format(len(fuels), fuels)
            )
            raise RuntimeError(
                "Cannot break {0} into multiple fuel components b/c there is not a single fuel"
                " component.".format(self)
            )

        fuel = fuels[0]
        fuelFlags = fuel.p.flags
        nPins = self.getNumPins()
        runLog.info(
            "Creating {} individual {} components on {}".format(nPins, fuel, self)
        )

        # Handle all other components that may be linked to the fuel multiplicity
        # by unlinking them and setting them directly.
        # TODO: What about other (actual) dimensions? This is a limitation in that only fuel
        # compuents are duplicated, and not the entire pin. It is also a reasonable assumption with
        # current/historical usage of ARMI.
        for comp, dim in self.getComponentsThatAreLinkedTo(fuel, "mult"):
            comp.setDimension(dim, nPins)

        # finish the first pin as a single pin
        fuel.setDimension("mult", 1)
        fuel.setName("fuel001")
        fuel.p.pinNum = 1

        # create all the new pin components and add them to the block with 'fuel001' names
        for i in range(nPins - 1):
            # wow, only use of a non-deepcopy
            newC = copy.copy(fuel)
            newC.setName("fuel{0:03d}".format(i + 2))  # start with 002.
            newC.p.pinNum = i + 2
            self.add(newC)

        # update moles at BOL for each pin
        self.p.molesHmBOLByPin = []
        for pin in self.iterComponents(Flags.FUEL):
            # Update the fuel component flags to be the same as before the split (i.e., DEPLETABLE)
            pin.p.flags = fuelFlags
            self.p.molesHmBOLByPin.append(pin.getHMMoles())
            pin.p.massHmBOL /= nPins

    def getIntegratedMgFlux(self, adjoint=False, gamma=False):
        """
        Return the volume integrated multigroup neutron tracklength in [n-cm/s].

        The first entry is the first energy group (fastest neutrons). Each additional
        group is the next energy group, as set in the ISOTXS library.

        Parameters
        ----------
        adjoint : bool, optional
            Return adjoint flux instead of real

        gamma : bool, optional
            Whether to return the neutron flux or the gamma flux.

        Returns
        -------
        integratedFlux : np.ndarray
            multigroup neutron tracklength in [n-cm/s]
        """
        if adjoint:
            if gamma:
                raise ValueError("Adjoint gamma flux is currently unsupported.")
            integratedFlux = self.p.adjMgFlux
        elif gamma:
            integratedFlux = self.p.mgFluxGamma
        else:
            integratedFlux = self.p.mgFlux

        return np.array(integratedFlux)

    def getLumpedFissionProductCollection(self):
        """
        Get collection of LFP objects. Will work for global or block-level LFP models.

        Returns
        -------
        lfps : LumpedFissionProduct
            lfpName keys , lfp object values

        See Also
        --------
        armi.physics.neutronics.fissionProductModel.lumpedFissionProduct.LumpedFissionProduct : LFP object
        """
        return composites.ArmiObject.getLumpedFissionProductCollection(self)

    def rotate(self, rad):
        """Function for rotating a block's spatially varying variables by a specified angle (radians).

        Parameters
        ----------
        rad: float
            Number (in radians) specifying the angle of counter clockwise rotation.
        """
        raise NotImplementedError

    def setAxialExpTargetComp(self, targetComponent):
        """Sets the targetComponent for the axial expansion changer.

        .. impl:: Set the target axial expansion components on a given block.
            :id: I_ARMI_MANUAL_TARG_COMP
            :implements: R_ARMI_MANUAL_TARG_COMP

            Sets the ``axialExpTargetComponent`` parameter on the block to the name of the Component
            which is passed in. This is then used by the
            :py:class:`~armi.reactor.converters.axialExpansionChanger.AxialExpansionChanger`
            class during axial expansion.

            This method is typically called from within
            :py:meth:`~armi.reactor.blueprints.blockBlueprint.BlockBlueprint.construct` during the
            process of building a Block from the blueprints.

        Parameter
        ---------
        targetComponent: :py:class:`Component <armi.reactor.components.component.Component>` object
            Component specified to be target component for axial expansion changer

        See Also
        --------
        armi.reactor.converters.axialExpansionChanger.py::ExpansionData::_setTargetComponents
        """
        self.p.axialExpTargetComponent = targetComponent.name

    def getPinCoordinates(self):
        """
        Compute the local centroid coordinates of any pins in this block.

        The pins must have a CLAD-flagged component for this to work.

        Returns
        -------
        localCoordinates : list
            list of (x,y,z) pairs representing each pin in the order they are listed as children

        Notes
        -----
        This assumes hexagonal pin lattice and needs to be upgraded once more generic geometry
        options are needed. Only works if pins have clad.
        """
        coords = []
        for clad in self.getChildrenWithFlags(Flags.CLAD):
            if isinstance(clad.spatialLocator, grids.MultiIndexLocation):
                coords.extend(
                    [locator.getLocalCoordinates() for locator in clad.spatialLocator]
                )
            else:
                coords.append(clad.spatialLocator.getLocalCoordinates())
        return coords

    def getTotalEnergyGenerationConstants(self):
        """
        Get the total energy generation group constants for a block.

        Gives the total energy generation rates when multiplied by the multigroup flux.

        Returns
        -------
        totalEnergyGenConstant: np.ndarray
            Total (fission + capture) energy generation group constants (Joules/cm)
        """
        return (
            self.getFissionEnergyGenerationConstants()
            + self.getCaptureEnergyGenerationConstants()
        )

    def getFissionEnergyGenerationConstants(self):
        """
        Get the fission energy generation group constants for a block.

        Gives the fission energy generation rates when multiplied by the multigroup
        flux.

        Returns
        -------
        fissionEnergyGenConstant: np.ndarray
            Energy generation group constants (Joules/cm)

        Raises
        ------
        RuntimeError:
            Reports if a cross section library is not assigned to a reactor.
        """
        if not self.core.lib:
            raise RuntimeError(
                "Cannot compute energy generation group constants without a library"
                ". Please ensure a library exists."
            )

        return xsCollections.computeFissionEnergyGenerationConstants(
            self.getNumberDensities(), self.core.lib, self.getMicroSuffix()
        )

    def getCaptureEnergyGenerationConstants(self):
        """
        Get the capture energy generation group constants for a block.

        Gives the capture energy generation rates when multiplied by the multigroup
        flux.

        Returns
        -------
        fissionEnergyGenConstant: np.ndarray
            Energy generation group constants (Joules/cm)

        Raises
        ------
        RuntimeError:
            Reports if a cross section library is not assigned to a reactor.
        """
        if not self.core.lib:
            raise RuntimeError(
                "Cannot compute energy generation group constants without a library"
                ". Please ensure a library exists."
            )

        return xsCollections.computeCaptureEnergyGenerationConstants(
            self.getNumberDensities(), self.core.lib, self.getMicroSuffix()
        )

    def getNeutronEnergyDepositionConstants(self):
        """
        Get the neutron energy deposition group constants for a block.

        Returns
        -------
        energyDepConstants: np.ndarray
            Neutron energy generation group constants (in Joules/cm)

        Raises
        ------
        RuntimeError:
            Reports if a cross section library is not assigned to a reactor.
        """
        if not self.core.lib:
            raise RuntimeError(
                "Cannot get neutron energy deposition group constants without "
                "a library. Please ensure a library exists."
            )

        return xsCollections.computeNeutronEnergyDepositionConstants(
            self.getNumberDensities(), self.core.lib, self.getMicroSuffix()
        )

    def getGammaEnergyDepositionConstants(self):
        """
        Get the gamma energy deposition group constants for a block.

        Returns
        -------
        energyDepConstants: np.ndarray
            Energy generation group constants (in Joules/cm)

        Raises
        ------
        RuntimeError:
            Reports if a cross section library is not assigned to a reactor.
        """
        if not self.core.lib:
            raise RuntimeError(
                "Cannot get gamma energy deposition group constants without "
                "a library. Please ensure a library exists."
            )

        return xsCollections.computeGammaEnergyDepositionConstants(
            self.getNumberDensities(), self.core.lib, self.getMicroSuffix()
        )

    def getBoronMassEnrich(self):
        """Return B-10 mass fraction."""
        b10 = self.getMass("B10")
        b11 = self.getMass("B11")
        total = b11 + b10
        if total == 0.0:
            return 0.0
        return b10 / total

    def getPuMoles(self):
        """Returns total number of moles of Pu isotopes."""
        nucNames = [nuc.name for nuc in elements.byZ[94].nuclides]
        puN = sum(self.getNuclideNumberDensities(nucNames))

        return (
            puN
            / units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM
            * self.getVolume()
            * self.getSymmetryFactor()
        )

    def getUraniumMassEnrich(self):
        """Returns U-235 mass fraction assuming U-235 and U-238 only."""
        u5 = self.getMass("U235")
        if u5 < 1e-10:
            return 0.0
        u8 = self.getMass("U238")
        return u5 / (u8 + u5)


class HexBlock(Block):
    """
    Defines a HexBlock.

    .. impl:: ARMI has the ability to create hex shaped blocks.
        :id: I_ARMI_BLOCK_HEX
        :implements: R_ARMI_BLOCK_HEX

        This class defines hexagonal-shaped Blocks. It inherits functionality from the parent class,
        Block, and defines hexagonal-specific methods including, but not limited to, querying pin
        pitch, pin linear power densities, hydraulic diameter, and retrieving inner and outer pitch.
    """

    PITCH_COMPONENT_TYPE: ClassVar[_PitchDefiningComponent] = (components.Hexagon,)

    def __init__(self, name, height=1.0):
        Block.__init__(self, name, height)

    def coords(self):
        """
        Returns the coordinates of the block.

        .. impl:: Coordinates of a block are queryable.
            :id: I_ARMI_BLOCK_POSI2
            :implements: R_ARMI_BLOCK_POSI

            Calls to the :py:meth:`~armi.reactor.grids.locations.IndexLocation.getGlobalCoordinates`
            method of the block's ``spatialLocator`` attribute, which recursively
            calls itself on all parents of the block to get the coordinates of the
            block's centroid in 3D cartesian space.

            Will additionally adjust the x and y coordinates based on the block
            parameters ``displacementX`` and ``displacementY``.
        """
        x, y, _z = self.spatialLocator.getGlobalCoordinates()
        x += self.p.displacementX * 100.0
        y += self.p.displacementY * 100.0
        return (
            round(x, units.FLOAT_DIMENSION_DECIMALS),
            round(y, units.FLOAT_DIMENSION_DECIMALS),
        )

    def createHomogenizedCopy(self, pinSpatialLocators=False):
        """
        Create a new homogenized copy of a block that is less expensive than a full deepcopy.

        .. impl:: Block compositions can be homogenized.
            :id: I_ARMI_BLOCK_HOMOG
            :implements: R_ARMI_BLOCK_HOMOG

            This method creates and returns a homogenized representation of itself in the form of a new Block.
            The homogenization occurs in the following manner. A single Hexagon Component is created
            and added to the new Block. This Hexagon Component is given the
            :py:class:`armi.materials.mixture._Mixture` material and a volume averaged temperature
            (``getAverageTempInC``). The number densities of the original Block are also stored on
            this new Component (:need:`I_ARMI_CMP_GET_NDENS`). Several parameters from the original block
            are copied onto the homogenized block (e.g., macros, lumped fission products, burnup group,
            number of pins, and spatial grid).

        Notes
        -----
        This can be used to improve performance when a new copy of a reactor needs to be
        built, but the full detail of the block (including component geometry, material,
        number density, etc.) is not required for the targeted physics solver being applied
        to the new reactor model.

        The main use case is for the uniform mesh converter (UMC). Frequently, a deterministic
        neutronics solver will require a uniform mesh reactor, which is produced by the UMC.
        Many deterministic solvers for fast spectrum reactors will also treat the individual
        blocks as homogenized mixtures. Since the neutronics solver does not need to know about
        the geometric and material details of the individual child components within a block,
        we can save significant effort while building the uniform mesh reactor with the UMC
        by omitting this detailed data and only providing the necessary level of detail for
        the uniform mesh reactor: number densities on each block.

        Individual components within a block can have different temperatures, and this
        can affect cross sections. This temperature variation is captured by the lattice physics
        module. As long as temperature distribution is correctly captured during cross section
        generation, it doesn't need to be transferred to the neutronics solver directly through
        this copy operation.

        If you make a new block, you must add it to an assembly and a reactor.

        Returns
        -------
        b
            A homogenized block containing a single Hexagon Component that contains an
            average temperature and the number densities from the original block.

        See Also
        --------
        armi.reactor.converters.uniformMesh.UniformMeshGeometryConverter.makeAssemWithUniformMesh
        """
        b = self.__class__(self.getName(), height=self.getHeight())
        b.setType(self.getType(), self.p.flags)

        # assign macros and LFP
        b.macros = self.macros
        b._lumpedFissionProducts = self._lumpedFissionProducts
        b.p.buGroup = self.p.buGroup

        hexComponent = Hexagon(
            "homogenizedHex",
            "_Mixture",
            self.getAverageTempInC(),
            self.getAverageTempInC(),
            self._pitchDefiningComponent[1],
        )
        hexComponent.setNumberDensities(self.getNumberDensities())
        b.add(hexComponent)

        b.p.nPins = self.p.nPins
        if pinSpatialLocators:
            # create a null component with cladding flags and spatialLocator from source block's
            # clad components in case pin locations need to be known for physics solver
            if self.hasComponents(Flags.CLAD):
                cladComponents = self.getComponents(Flags.CLAD)
                for i, clad in enumerate(cladComponents):
                    pinComponent = Circle(
                        f"voidPin{i}",
                        "Void",
                        self.getAverageTempInC(),
                        self.getAverageTempInC(),
                        0.0,
                    )
                    pinComponent.setType("pin", Flags.CLAD)
                    pinComponent.spatialLocator = copy.deepcopy(clad.spatialLocator)
                    if isinstance(
                        pinComponent.spatialLocator, grids.MultiIndexLocation
                    ):
                        for i1, i2 in zip(
                            list(pinComponent.spatialLocator), list(clad.spatialLocator)
                        ):
                            i1.associate(i2.grid)
                    pinComponent.setDimension("mult", clad.getDimension("mult"))
                    b.add(pinComponent)

        if self.spatialGrid is not None:
            b.spatialGrid = self.spatialGrid

        return b

    def getMaxArea(self):
        """
        Compute the max area of this block if it was totally full.

        .. impl:: Area of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS2
            :implements: R_ARMI_BLOCK_DIMS

            This method first retrieves the pitch of the hexagonal Block
            (:need:`I_ARMI_UTIL_HEXAGON0`) and then leverages the
            area calculation via :need:`I_ARMI_UTIL_HEXAGON0`.

        """
        pitch = self.getPitch()
        if not pitch:
            return 0.0
        return hexagon.area(pitch)

    def getDuctIP(self):
        """
        Returns the duct IP dimension.

        .. impl:: IP dimension is retrievable.
            :id: I_ARMI_BLOCK_DIMS3
            :implements: R_ARMI_BLOCK_DIMS

            This method retrieves the duct Component and quieries
            it's inner pitch directly. If the duct is missing or if there
            are multiple duct Components, an error will be raised.
        """
        duct = self.getComponent(Flags.DUCT, exact=True)
        return duct.getDimension("ip")

    def getDuctOP(self):
        """
        Returns the duct OP dimension.

        .. impl:: OP dimension is retrievable.
            :id: I_ARMI_BLOCK_DIMS4
            :implements: R_ARMI_BLOCK_DIMS

            This method retrieves the duct Component and quieries
            its outer pitch directly. If the duct is missing or if there
            are multiple duct Components, an error will be raised.
        """
        duct = self.getComponent(Flags.DUCT, exact=True)
        return duct.getDimension("op")

    def initializePinLocations(self):
        """Initialize pin locations."""
        nPins = self.getNumPins()
        self.p.pinLocation = list(range(1, nPins + 1))

    def setPinPowers(self, powers, powerKeySuffix=""):
        """
        Updates the pin linear power densities of this block for the current rotation.
        The linear densities are represented by the *linPowByPin* parameter.

        It is assumed that :py:meth:`.initializePinLocations` has already been executed
        for fueled blocks in order to access the *pinLocation* parameter. The
        *pinLocation* parameter is not accessed for non-fueled blocks.

        The *linPowByPin* parameter can be directly assigned to instead of using this
        method if the multiplicity of the pins in the block is equal to the number of
        pins in the block.

        Parameters
        ----------
        powers : list of floats, required
            The block-level pin linear power densities. powers[i] represents the average
            linear power density of pin i. The units of linear power density is watts/cm
            (i.e., watts produced per cm of pin length). The "ARMI pin ordering" must be
            be used, which is counter-clockwise from 3 o'clock.

        powerKeySuffix: str, optional
            Must be either an empty string, :py:const:`NEUTRON <armi.physics.neutronics.const.NEUTRON>`,
            or :py:const:`GAMMA <armi.physics.neutronics.const.GAMMA>`. Defaults to empty
            string.

        Notes
        -----
        This method can handle assembly rotations by using the *pinLocation* parameter.
        """
        numPins = self.getNumPins()
        if not numPins or numPins != len(powers):
            raise ValueError(
                f"Invalid power data for {self} with {numPins} pins."
                f" Got {len(powers)} entries in powers: {powers}"
            )

        powerKey = f"linPowByPin{powerKeySuffix}"
        self.p[powerKey] = np.zeros(numPins)

        # Loop through rings. The *pinLocation* parameter is only accessed for fueled
        # blocks; it is assumed that non-fueled blocks do not use a rotation map.
        for pinNum in range(numPins):
            if self.hasFlags(Flags.FUEL):
                # -1 is needed in order to map from pinLocations to list index
                pinLoc = self.p.pinLocation[pinNum] - 1
            else:
                pinLoc = pinNum
            pinLinPow = powers[pinLoc]
            self.p[powerKey][pinNum] = pinLinPow

        # If using the *powerKeySuffix* parameter, we also need to set total power, which
        # is sum of neutron and gamma powers. We assume that a solo gamma calculation
        # to set total power does not make sense.
        if powerKeySuffix:
            if powerKeySuffix == GAMMA:
                if self.p[f"linPowByPin{NEUTRON}"] is None:
                    msg = (
                        "Neutron power has not been set yet. Cannot set total power for "
                        f"{self}."
                    )
                    raise UnboundLocalError(msg)
                self.p.linPowByPin = self.p[f"linPowByPin{NEUTRON}"] + self.p[powerKey]
            else:
                self.p.linPowByPin = self.p[powerKey]

    def rotate(self, rad):
        """
        Rotates a block's spatially varying parameters by a specified angle in the
        counter-clockwise direction.

        The parameters must have a ParamLocation of either CORNERS or EDGES and must be a
        Python list of length 6 in order to be eligible for rotation; all parameters that
        do not meet these two criteria are not rotated.

        The pin indexing, as stored on the pinLocation parameter, is also updated via
        :py:meth:`rotatePins <armi.reactor.blocks.HexBlock.rotatePins>`.

        Parameters
        ----------
        rad: float, required
            Angle of counter-clockwise rotation in units of radians. Rotations must be
            in 60-degree increments (i.e., PI/6, PI/3, PI, 2 * PI/3, 5 * PI/6,
            and 2 * PI)

        See Also
        --------
        :py:meth:`rotatePins <armi.reactor.blocks.HexBlock.rotatePins>`
        """
        rotNum = round((rad % (2 * math.pi)) / math.radians(60))
        self.rotatePins(rotNum)
        params = self.p.paramDefs.atLocation(ParamLocation.CORNERS).names
        params += self.p.paramDefs.atLocation(ParamLocation.EDGES).names
        for param in params:
            if isinstance(self.p[param], list):
                if len(self.p[param]) == 6:
                    self.p[param] = self.p[param][-rotNum:] + self.p[param][:-rotNum]
                elif self.p[param] == []:
                    # List hasn't been defined yet, no warning needed.
                    pass
                else:
                    msg = (
                        "No rotation method defined for spatial parameters that aren't "
                        "defined once per hex edge/corner. No rotation performed "
                        f"on {param}"
                    )
                    runLog.warning(msg)
            elif isinstance(self.p[param], np.ndarray):
                if len(self.p[param]) == 6:
                    self.p[param] = np.concatenate(
                        (self.p[param][-rotNum:], self.p[param][:-rotNum])
                    )
                elif len(self.p[param]) == 0:
                    # Hasn't been defined yet, no warning needed.
                    pass
                else:
                    msg = (
                        "No rotation method defined for spatial parameters that aren't "
                        "defined once per hex edge/corner. No rotation performed "
                        f"on {param}"
                    )
                    runLog.warning(msg)
            elif isinstance(self.p[param], (int, float)):
                # this is a scalar and there shouldn't be any rotation.
                pass
            elif self.p[param] is None:
                # param is not set yet. no rotations as well.
                pass
            else:
                raise TypeError(
                    f"b.rotate() method received unexpected data type for {param} on block {self}\n"
                    + f"expected list, np.ndarray, int, or float. received {self.p[param]}"
                )
        # This specifically uses the .get() functionality to avoid an error if this
        # parameter does not exist.
        dispx = self.p.get("displacementX")
        dispy = self.p.get("displacementY")
        if (dispx is not None) and (dispy is not None):
            self.p.displacementX = dispx * math.cos(rad) - dispy * math.sin(rad)
            self.p.displacementY = dispx * math.sin(rad) + dispy * math.cos(rad)

    def rotatePins(self, rotNum, justCompute=False):
        """
        Rotate the pins of a block, which means rotating the indexing of pins. Note that this does
        not rotate all block quantities, just the pins.

        Parameters
        ----------
        rotNum : int, required
            An integer from 0 to 5, indicating the number of counterclockwise 60-degree rotations
            from the CURRENT orientation. Degrees of counter-clockwise rotation = 60*rot

        justCompute : boolean, optional
            If True, rotateIndexLookup will be returned but NOT assigned to the object parameter
            self.p.pinLocation. If False, rotateIndexLookup will be returned AND assigned to the
            object variable self.p.pinLocation.  Useful for figuring out which rotation is best
            to minimize burnup, etc.

        Returns
        -------
        rotateIndexLookup : dict of ints
            This is an index lookup (or mapping) between pin ids and pin locations. The pin
            indexing is 1-D (not ring,pos or GEODST). The "ARMI pin ordering" is used for location,
            which is counter-clockwise from 1 o'clock. Pin ids are always consecutively
            ordered starting at 1, while pin locations are not once a rotation has been
            applied.

        Notes
        -----
        Changing (x,y) positions of pins does NOT constitute rotation, because the indexing of pin
        atom densities must be re-ordered.  Re-order indexing of pin-level quantities, NOT (x,y)
        locations of pins.  Otherwise, subchannel input will be in wrong order.

        How rotations works is like this. There are pins with unique pin numbers in each block.
        These pin numbers will not change no matter what happens to a block, so if you have pin 1,
        you always have pin 1. However, these pins are all in pinLocations, and these are what
        change with rotations. At BOL, a pin's pinLocation is equal to its pin number, but after
        a rotation, this will no longer be so.

        So, all params that don't care about exactly where in space the pin is (such as depletion)
        can just use the pin number, but anything that needs to know the spatial location (such as
        fluxRecon, which interpolates the flux spatially, or subchannel codes, which needs to know where the
        power is) need to map through the pinLocation parameters.

        This method rotates the pins by changing the pinLocation parameter.

        See Also
        --------
        armi.reactor.blocks.HexBlock.rotate
            Rotates the entire block (pins, ducts, and spatial quantities).

        Examples
        --------
            rotateIndexLookup[i_after_rotation-1] = i_before_rotation-1
        """
        if not 0 <= rotNum <= 5:
            raise ValueError(
                "Cannot rotate {0} to rotNum {1}. Must be 0-5. ".format(self, rotNum)
            )

        # Pin numbers start at 1. Number of pins in the block is assumed to be based on
        # cladding count.
        numPins = self.getNumComponents(Flags.CLAD)
        rotateIndexLookup = dict(zip(range(1, numPins + 1), range(1, numPins + 1)))

        # Look up the current orientation and add this to it. The math below just rotates
        # from the reference point so we need a total rotation.
        rotNum = int((self.getRotationNum() + rotNum) % 6)

        # non-trivial rotation requested
        # start at 2 because pin 1 never changes (it's in the center!)
        for pinNum in range(2, numPins + 1):
            if rotNum == 0:
                # Rotation to reference orientation. Pin locations are pin IDs.
                pass
            else:
                # Determine the pin ring. Rotation does not change the pin ring!
                ring = int(
                    math.ceil((3.0 + math.sqrt(9.0 - 12.0 * (1.0 - pinNum))) / 6.0)
                )

                # Rotate the pin position (within the ring, which does not change)
                tot_pins = 1 + 3 * ring * (ring - 1)
                newPinLocation = pinNum + (ring - 1) * rotNum
                if newPinLocation > tot_pins:
                    newPinLocation -= (ring - 1) * 6

                # Assign "before" and "after" pin indices to the index lookup
                rotateIndexLookup[pinNum] = newPinLocation

        # Because the above math creates indices based on the absolute rotation number,
        # the old values of pinLocation (if they've been set in the past) can be overwritten
        # with new numbers
        if not justCompute:
            self.setRotationNum(rotNum)
            self.p["pinLocation"] = [
                rotateIndexLookup[pinNum] for pinNum in range(1, numPins + 1)
            ]

        return rotateIndexLookup

    def verifyBlockDims(self):
        """Perform some checks on this type of block before it is assembled."""
        try:
            wireComp = self.getComponent(Flags.WIRE)
            ductComps = self.getComponents(Flags.DUCT)
            cladComp = self.getComponent(Flags.CLAD)
        except ValueError:
            # there are probably more that one clad/wire, so we really dont know what this block looks like
            runLog.info(
                "Block design {} is too complicated to verify dimensions. Make sure they "
                "are correct!".format(self)
            )
            return
        # check wire wrap in contact with clad
        if (
            self.getComponent(Flags.CLAD) is not None
            and self.getComponent(Flags.WIRE) is not None
        ):
            wwCladGap = self.getWireWrapCladGap(cold=True)
            if round(wwCladGap, 6) != 0.0:
                runLog.warning(
                    "The gap between wire wrap and clad in block {} was {} cm. Expected 0.0."
                    "".format(self, wwCladGap),
                    single=True,
                )
        # check clad duct overlap
        pinToDuctGap = self.getPinToDuctGap(cold=True)
        # Allow for some tolerance; user input precision may lead to slight negative
        # gaps
        if pinToDuctGap is not None and pinToDuctGap < -0.005:
            raise ValueError(
                "Gap between pins and duct is {0:.4f} cm in {1}. Make more room.".format(
                    pinToDuctGap, self
                )
            )
        elif pinToDuctGap is None:
            # only produce a warning if pin or clad are found, but not all of pin, clad and duct. We
            # may need to tune this logic a bit
            ductComp = next(iter(ductComps), None)
            if (cladComp is not None or wireComp is not None) and any(
                [c is None for c in (wireComp, cladComp, ductComp)]
            ):
                runLog.warning(
                    "Some component was missing in {} so pin-to-duct gap not calculated"
                    "".format(self)
                )

    def getPinToDuctGap(self, cold=False):
        """
        Returns the distance in cm between the outer most pin and the duct in a block.

        .. impl:: Pin to duct gap of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS5
            :implements: R_ARMI_BLOCK_DIMS

            Requires that the outer most duct be Hexagonal and wire and clad Components
            be present. The flat-to-flat distance between the radial exterior of opposing
            pins in the outermost ring is computed by computing the distance between
            pin centers (``getPinCenterFlatToFlat``) and adding the outer diameter of
            the clad Component and the outer diameter of the wire Component twice. The
            total margin between the inner pitch of the duct Component and the wire is then
            computed. The pin to duct gap is then half this distance.

        Parameters
        ----------
        cold : boolean
            Determines whether the results should be cold or hot dimensions.

        Returns
        -------
        pinToDuctGap : float
            Returns the diameteral gap between the outer most pins in a hex pack to the duct inner
            face to face in cm.
        """
        wire = self.getComponent(Flags.WIRE)
        ducts = sorted(self.getChildrenWithFlags(Flags.DUCT))
        duct = None
        if any(ducts):
            duct = ducts[0]
            if not isinstance(duct, components.Hexagon):
                # getPinCenterFlatToFlat only works for hexes
                # inner most duct might be circle or some other shape
                duct = None
            elif isinstance(duct, components.HoledHexagon):
                # has no ip and is circular on inside so following
                # code will not work
                duct = None
        clad = self.getComponent(Flags.CLAD)
        if any(c is None for c in (duct, wire, clad)):
            return None

        # note, if nRings was a None, this could be for a non-hex packed fuel assembly
        # see thermal hydraulic design basis for description of equation
        pinCenterFlatToFlat = self.getPinCenterFlatToFlat(cold=cold)
        pinOuterFlatToFlat = (
            pinCenterFlatToFlat
            + clad.getDimension("od", cold=cold)
            + 2.0 * wire.getDimension("od", cold=cold)
        )
        ductMarginToContact = duct.getDimension("ip", cold=cold) - pinOuterFlatToFlat
        pinToDuctGap = ductMarginToContact / 2.0

        return pinToDuctGap

    def getRotationNum(self):
        """Get index 0 through 5 indicating number of rotations counterclockwise around the z-axis."""
        return (
            np.rint(self.p.orientation[2] / 360.0 * 6) % 6
        )  # assume rotation only in Z

    def setRotationNum(self, rotNum):
        """
        Set orientation based on a number 0 through 5 indicating number of rotations
        counterclockwise around the z-axis.
        """
        self.p.orientation[2] = 60.0 * rotNum

    def getSymmetryFactor(self):
        """
        Return a factor between 1 and N where 1/N is how much cut-off by symmetry lines this mesh
        cell is.

        Reactor-level meshes have symmetry information so we have a reactor for this to work. That's
        why it's not implemented on the grid/locator level.

        When edge-assemblies are included on both edges (i.e. MCNP or DIF3D-FD 1/3-symmetric cases),
        the edge assemblies have symmetry factors of 2.0. Otherwise (DIF3D-nodal) there's a full
        assembly on the bottom edge (overhanging) and no assembly at the top edge so the ones at the
        bottom are considered full (symmetryFactor=1).

        If this block is not in any grid at all, then there can be no symmetry so return 1.
        """
        try:
            symmetry = self.parent.spatialLocator.grid.symmetry
        except Exception:
            return 1.0
        if (
            symmetry.domain == geometry.DomainType.THIRD_CORE
            and symmetry.boundary == geometry.BoundaryType.PERIODIC
        ):
            indices = self.spatialLocator.getCompleteIndices()
            if indices[0] == 0 and indices[1] == 0:
                # central location
                return 3.0
            else:
                symmetryLine = self.core.spatialGrid.overlapsWhichSymmetryLine(indices)
                # detect if upper edge assemblies are included. Doing this is the only way to know
                # definitively whether or not the edge assemblies are half-assems or full.
                # seeing the first one is the easiest way to detect them.
                # Check it last in the and statement so we don't waste time doing it.
                upperEdgeLoc = self.core.spatialGrid[-1, 2, 0]
                if symmetryLine in [
                    grids.BOUNDARY_0_DEGREES,
                    grids.BOUNDARY_120_DEGREES,
                ] and bool(self.core.childrenByLocator.get(upperEdgeLoc)):
                    return 2.0
        return 1.0

    def autoCreateSpatialGrids(self):
        """
        Given a block without a spatialGrid, create a spatialGrid and give its children the
        corresponding spatialLocators (if it is a simple block).

        In this case, a simple block would be one that has either multiplicity of components equal
        to 1 or N but no other multiplicities. Also, this should only happen when N fits exactly
        into a given number of hex rings.  Otherwise, do not create a grid for this block.

        Notes
        -----
        If the Block meets all the conditions, we gather all components to either be a
        multiIndexLocation containing all of the pin positions, or the locator is the center (0,0).

        Also, this only works on blocks that have 'flat side up'.

        Raises
        ------
        ValueError
            If the multiplicities of the block are not only 1 or N or if generated ringNumber leads
            to more positions than necessary.
        """
        # Check multiplicities...
        mults = {c.getDimension("mult") for c in self.iterComponents()}

        if len(mults) != 2 or 1 not in mults:
            raise ValueError(
                "Could not create a spatialGrid for block {}, multiplicities are not 1 or N they are {}".format(
                    self.p.type, mults
                )
            )

        ringNumber = hexagon.numRingsToHoldNumCells(self.getNumPins())
        # For the below to work, there must not be multiple wire or multiple clad types.
        # note that it's the pointed end of the cell hexes that are up (but the
        # macro shape of the pins forms a hex with a flat top fitting in the assembly)
        grid = grids.HexGrid.fromPitch(
            self.getPinPitch(cold=True), numRings=0, cornersUp=True
        )
        spatialLocators = grids.MultiIndexLocation(grid=self.spatialGrid)
        numLocations = 0
        for ring in range(ringNumber):
            numLocations = numLocations + hexagon.numPositionsInRing(ring + 1)
        if numLocations != self.getNumPins():
            raise ValueError(
                "Cannot create spatialGrid, number of locations in rings{} not equal to pin number{}".format(
                    numLocations, self.getNumPins()
                )
            )

        i = 0
        for ring in range(ringNumber):
            for pos in range(grid.getPositionsInRing(ring + 1)):
                i, j = grid.getIndicesFromRingAndPos(ring + 1, pos + 1)
                spatialLocators.append(grid[i, j, 0])
        if self.spatialGrid is None:
            self.spatialGrid = grid
            for c in self:
                if c.getDimension("mult") > 1:
                    c.spatialLocator = spatialLocators
                elif c.getDimension("mult") == 1:
                    c.spatialLocator = grids.CoordinateLocation(0.0, 0.0, 0.0, grid)

    def getPinCenterFlatToFlat(self, cold=False):
        """Return the flat-to-flat distance between the centers of opposing pins in the outermost ring."""
        clad = self.getComponent(Flags.CLAD)
        nRings = hexagon.numRingsToHoldNumCells(clad.getDimension("mult"))
        pinPitch = self.getPinPitch(cold=cold)
        pinCenterCornerToCorner = 2 * (nRings - 1) * pinPitch
        pinCenterFlatToFlat = math.sqrt(3.0) / 2.0 * pinCenterCornerToCorner
        return pinCenterFlatToFlat

    def hasPinPitch(self):
        """Return True if the block has enough information to calculate pin pitch."""
        return (self.getComponent(Flags.CLAD) is not None) and (
            self.getComponent(Flags.WIRE) is not None
        )

    def getPinPitch(self, cold=False):
        """
        Get the pin pitch in cm.

        Assumes that the pin pitch is defined entirely by contacting cladding tubes and wire wraps.
        Grid spacers not yet supported.

        .. impl:: Pin pitch within block is retrievable.
            :id: I_ARMI_BLOCK_DIMS6
            :implements: R_ARMI_BLOCK_DIMS

            This implementation requires that clad and wire Components are present.
            If not, an error is raised. If present, the pin pitch is calculated
            as the sum of the outer diameter of the clad and outer diameter of
            the wire.

        Parameters
        ----------
        cold : boolean
            Determines whether the dimensions should be cold or hot

        Returns
        -------
        pinPitch : float
            pin pitch in cm
        """
        try:
            clad = self.getComponent(Flags.CLAD)
            wire = self.getComponent(Flags.WIRE)
        except ValueError:
            raise ValueError(
                "Block {} has multiple clad and wire components,"
                " so pin pitch is not well-defined.".format(self)
            )

        if wire and clad:
            return clad.getDimension("od", cold=cold) + wire.getDimension(
                "od", cold=cold
            )
        else:
            raise ValueError(
                "Cannot get pin pitch in {} because it does not have a wire and a clad".format(
                    self
                )
            )

    def getWettedPerimeter(self):
        r"""Return the total wetted perimeter of the block in cm.

        .. impl:: Wetted perimeter of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS7
            :implements: R_ARMI_BLOCK_DIMS

            This implementation computes wetted perimeters for specific Components, as specified
            by their Flags (:need:`R_ARMI_FLAG_DEFINE`). Hollow hexagons and circular pin Components
            are supported. The latter supports both instances where the exterior is wetted
            (e.g., clad, wire) as well as when the interior and exterior are wetted (hollow circle).

            Hollow hexagons are calculated via,

            .. math::

                \frac{6 \times \text{ip}}{\sqrt{3}},

            where :math:`\text{ip}` is the inner pitch of the hollow hexagon. Circular pin Components
            where the exterior is wetted is calculated via,

            .. math::

                N \pi \left( \text{OD}_c + \text{OD}_w \right),

            where :math:`N` is the total number of pins, :math:`\text{OD}_c` is the outer diameter
            of the clad, and :math:`\text{OD}_w` is the outer diameter of the wire, respectively.
            When both the interior and exterior are wetted, the wetted perimeter is calculated as

            .. math::

                \pi \left( \text{OD} + \text{ID} \right),

            where :math:`\text{OD}` and :math:`\text{ID}` are the outer and inner diameters of the pin
            Component, respectively.

        """
        # flags pertaining to hexagon components where the interior of the hexagon is wetted
        wettedHollowHexagonComponentFlags = (
            Flags.DUCT,
            Flags.GRID_PLATE,
            Flags.INLET_NOZZLE,
            Flags.HANDLING_SOCKET,
        )

        # flags pertaining to circular pin components where the exterior of the circle is wetted
        wettedPinComponentFlags = (
            Flags.CLAD,
            Flags.WIRE,
        )

        # flags pertaining to circular components where both the interior and exterior of the circle are wetted
        wettedHollowCircleComponentFlags = (Flags.DUCT | Flags.INNER,)

        # obtain all wetted components based on type
        wettedHollowHexagonComponents = []
        for flag in wettedHollowHexagonComponentFlags:
            c = self.getComponent(flag, exact=True)
            wettedHollowHexagonComponents.append(c) if c else None

        wettedPinComponents = []
        for flag in wettedPinComponentFlags:
            c = self.getComponent(flag, exact=True)
            wettedPinComponents.append(c) if c else None

        wettedHollowCircleComponents = []
        for flag in wettedHollowCircleComponentFlags:
            c = self.getComponent(flag, exact=True)
            wettedHollowCircleComponents.append(c) if c else None

        # calculate wetted perimeters according to their geometries

        # hollow hexagon = 6 * ip / sqrt(3)
        wettedHollowHexagonPerimeter = 0.0
        for c in wettedHollowHexagonComponents:
            wettedHollowHexagonPerimeter += (
                6 * c.getDimension("ip") / math.sqrt(3) if c else 0.0
            )

        # solid circle = NumPins * pi * (Comp Diam + Wire Diam)
        wettedPinPerimeter = 0.0
        for c in wettedPinComponents:
            correctionFactor = 1.0
            if isinstance(c, Helix):
                # account for the helical wire wrap
                correctionFactor = np.hypot(
                    1.0,
                    math.pi
                    * c.getDimension("helixDiameter")
                    / c.getDimension("axialPitch"),
                )
            wettedPinPerimeter += c.getDimension("od") * correctionFactor
        wettedPinPerimeter *= self.getNumPins() * math.pi

        # hollow circle = (id + od) * pi
        wettedHollowCirclePerimeter = 0.0
        for c in wettedHollowCircleComponents:
            wettedHollowCirclePerimeter += (
                c.getDimension("id") + c.getDimension("od") if c else 0.0
            )
        wettedHollowCirclePerimeter *= math.pi

        return (
            wettedHollowHexagonPerimeter
            + wettedPinPerimeter
            + wettedHollowCirclePerimeter
        )

    def getFlowArea(self):
        """Return the total flowing coolant area of the block in cm^2.

        .. impl:: Flow area of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS8
            :implements: R_ARMI_BLOCK_DIMS

            Retrieving the flow area requires that there be a single coolant Component.
            If available, the area is calculated (:need:`I_ARMI_COMP_VOL0`).
        """
        return self.getComponent(Flags.COOLANT, exact=True).getArea()

    def getHydraulicDiameter(self):
        r"""
        Return the hydraulic diameter in this block in cm.

        Hydraulic diameter is 4A/P where A is the flow area and P is the wetted perimeter.
        In a hex assembly, the wetted perimeter includes the cladding, the wire wrap, and the
        inside of the duct. The flow area is the inner area of the duct minus the area of the
        pins and the wire.

        .. impl:: Hydraulic diameter of block is retrievable.
            :id: I_ARMI_BLOCK_DIMS9
            :implements: R_ARMI_BLOCK_DIMS

            The hydraulic diamter is calculated via

            .. math::

                4\frac{A}{P},

            where :math:`A` is the flow area (:need:`I_ARMI_BLOCK_DIMS8`) and :math:`P` is the
            wetted perimeter (:need:`I_ARMI_BLOCK_DIMS7`).
        """
        return 4.0 * self.getFlowArea() / self.getWettedPerimeter()


class CartesianBlock(Block):
    PITCH_DIMENSION = "widthOuter"
    PITCH_COMPONENT_TYPE = components.Rectangle

    def getMaxArea(self):
        """Get area of this block if it were totally full."""
        xw, yw = self.getPitch()
        return xw * yw

    def setPitch(self, val, updateBolParams=False):
        raise NotImplementedError(
            "Directly setting the pitch of a cartesian block is currently not supported."
        )

    def getSymmetryFactor(self):
        """
        Return a factor between 1 and N where 1/N is how much cut-off by symmetry lines this mesh
        cell is.
        """
        if self.core is not None:
            indices = self.spatialLocator.getCompleteIndices()
            if self.core.symmetry.isThroughCenterAssembly:
                if indices[0] == 0 and indices[1] == 0:
                    # central location
                    return 4.0
                elif indices[0] == 0 or indices[1] == 0:
                    # edge location
                    return 2.0
        return 1.0

    def getPinCenterFlatToFlat(self, cold=False):
        """Return the flat-to-flat distance between the centers of opposing pins in the outermost ring."""
        clad = self.getComponent(Flags.CLAD)
        nRings = hexagon.numRingsToHoldNumCells(clad.getDimension("mult"))
        pinPitch = self.getPinPitch(cold=cold)
        return 2 * (nRings - 1) * pinPitch


class ThRZBlock(Block):
    # be sure to fill ThRZ blocks with only 3D components - components with explicit getVolume methods

    def getMaxArea(self):
        """Return the area of the Theta-R-Z block if it was totally full."""
        raise NotImplementedError(
            "Cannot get max area of a TRZ block. Fully specify your geometry."
        )

    def radialInner(self):
        """Return a smallest radius of all the components."""
        innerRadii = self.getDimensions("inner_radius")
        smallestInner = min(innerRadii) if innerRadii else None
        return smallestInner

    def radialOuter(self):
        """Return a largest radius of all the components."""
        outerRadii = self.getDimensions("outer_radius")
        largestOuter = max(outerRadii) if outerRadii else None
        return largestOuter

    def thetaInner(self):
        """Return a smallest theta of all the components."""
        innerTheta = self.getDimensions("inner_theta")
        smallestInner = min(innerTheta) if innerTheta else None
        return smallestInner

    def thetaOuter(self):
        """Return a largest theta of all the components."""
        outerTheta = self.getDimensions("outer_theta")
        largestOuter = max(outerTheta) if outerTheta else None
        return largestOuter

    def axialInner(self):
        """Return the lower z-coordinate."""
        return self.getDimensions("inner_axial")

    def axialOuter(self):
        """Return the upper z-coordinate."""
        return self.getDimensions("outer_axial")

    def verifyBlockDims(self):
        """Perform dimension checks related to ThetaRZ blocks."""
        return
