# coding: utf-8

import os

import numpy as np
from uuid import uuid4
from pymatgen.alchemy.materials import TransformedStructure
from pymatgen.transformations.advanced_transformations import (
    CubicSupercellTransformation,
    PerturbSitesTransformation
)
from fireworks import Workflow, Firework
from atomate.utils.utils import get_logger
from atomate.vasp.config import VASP_CMD, DB_FILE, ADD_WF_METADATA #                what is this?
from atomate.vasp.fireworks.core import StaticFW
from atomate.vasp.firetasks.parse_outputs import CSLDForceConstantsToDB
from atomate.vasp.powerups import (
    add_additional_fields_to_taskdocs
)

from pymatgen.io.vasp.sets import MPStaticSet

# NEW THINGS TO INSTALL?
# from configparser import ConfigParser
# from scripts import csld_main_rees #csld>scripts

logger = get_logger(__name__)

__author__ = "Rees Chang"
__email__ = "rc564@cornell.edu"
__date__ = "July 2019"

__csld_wf_version__ = 1.0

class CompressedSensingLatticeDynamicsWF:
    def __init__(
            self,
            parent_structure,
            min_atoms=-np.Inf,
            max_atoms=np.Inf,
            num_nn_dists=5,
            max_displacement=0.1,
            min_displacement=0.01,
            num_displacements=10,
            supercells_per_displacement_distance=1,
            min_random_distance=None,
            #csld input params here
    ):
        """
        This workflow will use compressed sensing lattice dynamics (CSLD)
        (doi: 10.1103/PhysRevLett.113.185501) to generate interatomic force
        constants from an input structure and output a summary to a database.

        A summary of the workflow is as follows:
            1. Transform the input structure into a supercell
            2. Transform the supercell into a list of supercells with all atoms
                randomly perturbed from their original sites
            3. Run static VASP calculations on each perturbed supercell to
                calculate atomic forces.
            4. Aggregate the forces and conduct the CSLD minimization algorithm
                to compute interatomic force constants.
            5. Output the interatomic force constants to the database.

        Args:
            parent_structure (Structure):
            min_atoms (int):
            max_atoms (int):
            num_nn_dists (int or float):
            max_displacement (float)
        """
        self.uuid = str(uuid4())
        self.wf_meta = {
            "wf_uuid": self.uuid,
            "wf_name": self.__class__.__name__, #"CompressedSensingLatticeDynamicsWF"
        }

    # Create supercell
        self.parent_structure = parent_structure
        self.min_atoms = min_atoms
        self.max_atoms = max_atoms
        self.num_nn_dists = num_nn_dists
        supercell_transform = CubicSupercellTransformation(
            self.min_atoms,
            self.max_atoms,
            self.num_nn_dists,
        )
        # supercell (Structure)
        self.supercell = supercell_transform.apply_transformation(self.parent_structure)
        self.trans_mat = supercell_transform.trans_mat

    # Generate randomly perturbed supercells
        perturbed_supercells_transform = PerturbSitesTransformation(
            max_displacement,
            min_displacement,
            num_displacements,
            supercells_per_displacement_distance,
            min_random_distance
        )
        # list of perturbed supercell structures (list)
        self.perturbed_supercells = perturbed_supercells_transform.apply_transformation(self.supercell)
        # list of (non-unique) displacement values used in the perturbation (np.ndarray)
        self.disps = np.repeat(perturbed_supercells_transform.disps,
                               supercells_per_displacement_distance)


    def get_wf(
            self,
            c=None
    ):
        fws = []
        c = c or {"VASP_CMD": VASP_CMD, "DB_FILE": DB_FILE}

        def _add_metadata(structure):
            """
            Add metadata for easy querying from the database later.
            """
            return TransformedStructure(
                structure, other_parameters={"wf_meta": self.wf_meta}
            )

        user_incar_settings = {"ADDGRID": True, #Fast Fourier Transform grid
                               "LCHARG": False,
                               "ENCUT": 700,
                               "EDIFF": 1e-7, #may need to tune this
                               "PREC": 'Accurate',
                               "LAECHG": False,
                               "LREAL": False,
                               "LASPH": True}
        user_incar_settings.update(c.get("user_incar_settings", {}))

        for idx, perturbed_supercell in enumerate(self.perturbed_supercells):
            # Run static calculations on the perturbed supercells to compute forces on each atom
            name = "perturbed supercell, idx: {}, disp_val: {:.3f},".format(idx, self.disps[idx])


            vis = MPStaticSet(perturbed_supercell,
                              user_incar_settings=user_incar_settings)

            fws.append(StaticFW(
                perturbed_supercell,
                vasp_input_set=vis,
                vasp_cmd=c["VASP_CMD"],
                db_file=c["DB_FILE"],
                name=name + " static"
            ))

        print('DISPS')
        print(self.disps)
        # Collect force constants from the DB and output on cluster
        csld_fw = Firework(
            CSLDForceConstantsToDB(
                db_file=c["DB_FILE"], # wot
                wf_uuid=self.uuid,
                name='CSLDForceConstantsToDB',
                parent_structure=self.parent_structure,
                trans_mat=self.trans_mat,
                supercell_structure=self.supercell,
                disps=self.disps
            ),
            name="Compressed Sensing Lattice Dynamics",
            parents=fws[-len(self.perturbed_supercells):]
        )
        fws.append(csld_fw)

        formula = self.parent_structure.composition.reduced_formula
        wf_name = "{} - compressed sensing lattice dynamics".format(formula)
        wf = Workflow(fws, name=wf_name)

        wf = add_additional_fields_to_taskdocs(wf,
                                               {"wf_meta": self.wf_meta},
                                               task_name_constraint="VaspToDb"
                                               #may need to change this to "CSLDForceConstantsToDB"?
                                               )
        #tag =   #insert anything relevant to every firework in the workflow
        # wf = add_tags(wf, [tag, <insert whatever string you'd like>])
        return wf


if __name__ == "__main__":

    from fireworks import LaunchPad
    from pymatgen.ext.matproj import MPRester

    #get a structure
    mpr = MPRester(api_key='auNIrJ23VLXCqbpl')
    structure = mpr.get_structure_by_material_id('mp-149')

    csld_class = CompressedSensingLatticeDynamicsWF(structure)
    # print(csld_class.disps)
    # print(len(csld_class.disps))

    wf = csld_class.get_wf()

    print(wf)

    lpad = LaunchPad.auto_load()
    lpad.add_wf(wf)

    #how did lpad know which database to put the wf?