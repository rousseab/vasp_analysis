"""
 Suite of functions to analyse my VASP generated data.
"""

import numpy as np
import os
import sys
import json
import re

import pymatgen

from pymatgen.serializers.json_coders import pmg_load
from pymatgen.apps.borg.hive import VaspToComputedEntryDrone
from pymatgen.apps.borg.queen import BorgQueen
from pymatgen.entries.compatibility import MaterialsProjectCompatibility

from pymatgen.phasediagram.pdmaker import PhaseDiagram
from pymatgen.phasediagram.pdanalyzer import PDAnalyzer


def compute_relative_energies(list_x_alkali,list_energies_per_unit,E0_E1=None):

    tol = 1e-8

    x_max = np.max(list_x_alkali)

    list_x = list_x_alkali/x_max

    if E0_E1 == None:
        I0 = np.where( np.abs(list_x_alkali) < tol )
        E0 = np.min(list_energies_per_unit[I0])

        I1 = np.where( np.abs(list_x_alkali-x_max) < tol )[0]
        E1 = np.min(list_energies_per_unit[I1])
    else:
        E0, E1 = E0_E1

    list_relative_E = list_energies_per_unit-list_x*E1-(1.-list_x)*E0


    list_x_min = []
    list_E_min = []

    tol = 1e-8
    for x in np.sort(list(set(list_x))):
        list_x_min.append(x)
        I = np.where( np.sqrt((list_x - x)**2) < tol)[0]
        list_E_min.append( np.min(list_relative_E[I]) )

    list_x_min = np.array(list_x_min )
    list_E_min = np.array(list_E_min )

    return list_x, list_relative_E, list_x_min, list_E_min, E0, E1

def compute_voltage(list_x_alkali,list_energies_per_unit,E_alkali):
    """
    This function computes voltage plateaus for compound 
    """

    #  Build the lowest energy curve
    list_xV = []
    list_EV = []
    for x in np.sort(list(set(list_x_alkali))):
        I = np.where(list_x_alkali== x)[0]

        list_EV.append(np.min(list_energies_per_unit[I]))
        list_xV.append(x)

    list_EV = np.array(list_EV)
    list_xV = np.array(list_xV)

    list_V = E_alkali - (list_EV[1:]-list_EV[:-1])/(list_xV[1:]-list_xV[:-1])

    list_x_plateau = []
    list_V_plateau = []

    for i,V in enumerate(list_V):
        x0 = list_xV[i]
        x1 = list_xV[i+1]

        list_x_plateau.append(x0)
        list_x_plateau.append(x1)

        list_V_plateau.append(V)
        list_V_plateau.append(V)

    return np.array(list_x_plateau), np.array(list_V_plateau)

class projected_DOS_reader(object):
    """
    This function wraps around the code I wrote to extract meaningful
    partial dos from what ASE extracts.
    """
    def __init__(self,DOSCAR_path,json_data_path):
        """
        Object requires paths to DOSCAR, a cif file containing the appropriate structure
        and the path to the appropriate run_data.json file, to extract the number of electrons
        """

        self.get_nelect_and_structure(json_data_path)
        self.read_DOSCAR(DOSCAR_path)


    def get_nelect_and_structure(self,json_data_path):

        with  open(json_data_path,'r') as f:
            data_dictionary = json.load(f)

        self.nelect = data_dictionary['OUTCAR']['nelect']
        self.EF_from_outcar = data_dictionary['OUTCAR']['efermi']
        self.structure = pymatgen.Structure.from_dict(data_dictionary['relaxation'][-1]['structure'])


    def read_DOSCAR(self,DOSCAR_path):

        doscar = VaspDos(DOSCAR_path)

        # consistency check
        number_of_atoms_DOSCAR_FILE = doscar._site_dos.shape[0]
        number_of_atoms_CIF_FILE = len(self.structure)

        if number_of_atoms_CIF_FILE  != number_of_atoms_DOSCAR_FILE: 
            print( 'Number of atoms in DOSCAR and CIF file are not equal!')
            print( 'inconsistent files; results would be meaningless')
            print('     *****   STOP  ******')
            sys.exit()

        self.energy = doscar.energy
        self.integrated_dos = np.sum(doscar.integrated_dos,axis=0)

        self.compute_EF()

        self.DOS_up =  doscar.dos[0,:]
        self.DOS_dn =  doscar.dos[1,:]

        indices_up = np.arange(0,18,2)
        indices_dn = np.arange(1,19,2)

        self.pdos_dict = {}
    
        for specie in self.structure.types_of_specie:

            pdos_up = np.zeros_like(self.DOS_up)
            pdos_dn = np.zeros_like(self.DOS_dn)

            for i,site in enumerate(self.structure):
                if site.specie == specie:

                    for iu in indices_up:
                        pdos_up += doscar.site_dos(i,iu)
                    for id in indices_dn: 
                        pdos_dn += doscar.site_dos(i,id)

            self.pdos_dict['%s_up'%specie.symbol] = pdos_up 
            self.pdos_dict['%s_dn'%specie.symbol] = pdos_dn



    def compute_EF(self):

        imax = np.where(self.integrated_dos >= self.nelect)[0][0]
        imin = np.where(self.integrated_dos < self.nelect)[0][-1]

        e1 = self.energy[imin]
        e2 = self.energy[imax]

        d1 = self.integrated_dos[imin]
        d2 = self.integrated_dos[imax]

        self.EF = ((e1-e2)*self.nelect+e2*d1-e1*d2)/(d1-d2)

class AnalyseJsonData():
    """
    Class which will easily extract data from "run_data.json" files.
    The expected json files are in my own format, wrapping up most  of
    the relevant information in a VASP vasprun.xml and OUTCAR files. 
    """

    def __init__(self,list_json_data_filenames):


        self.parse_json_data(list_json_data_filenames)

        return

    def parse_json_data(self,list_json_data_filenames):

        self.list_data_dictionaries = []
        self.list_structures        = []
        self.list_functional_json_data_filenames = []


        for json_data_filename in list_json_data_filenames:
            with open(json_data_filename ,'r') as f:
                try:
                    data_dictionary = json.load(f)

                    structure_dict = data_dictionary['relaxation'][-1]['structure']
                    structure = pymatgen.Structure.from_dict(structure_dict)

                    # make sure this exists, or else the file is broken
                    energy = data_dictionary['relaxation'][-1]['electronic']['e_0_energy']

                except:
                    print( 'file %s is broken'%json_data_filename)
                    continue                        
            # if file is not broken, tabulate data

            self.list_functional_json_data_filenames.append(json_data_filename) 
            self.list_data_dictionaries.append(data_dictionary) 
            self.list_structures.append(structure)


        return


    def extract_composition(self):

        list_compositions = []        
        for structure in self.list_structures:
            list_compositions.append(structure.composition)

        return list_compositions


    def extract_energies(self):

        list_energies = []
        for data_dictionary in self.list_data_dictionaries:
            energy = data_dictionary['relaxation'][-1]['electronic']['e_0_energy']
            list_energies.append(energy)

        return np.array(list_energies)


    def extract_magnetization(self,Element):
        MAG = []

        for data_dictionary, structure in zip(self.list_data_dictionaries,self.list_structures):
            list_d = data_dictionary['OUTCAR']['magnetization']                                                              
            list_mag = []

            for site, d in zip(structure,list_d):
                if site.specie == Element:
                    list_mag.append(d['tot'])

            MAG.append(list_mag)

        return MAG

class AnalyseMaterialsProjectJsonData():
    """
    Class which will wrap around boilerplate analysis of MaterialsProject-like
    json files, containing data extracted using borgs and queens.

    """

    def __init__(self):
        # some MP analysis power tools
        self.compat  = MaterialsProjectCompatibility()

        return

    def extract_alkali_energy(self, MP_alkali_json_data_filename):
        computed_entry = self._extract_MP_data(MP_alkali_json_data_filename)[0]
        processed_Alkali_entry = self.compat.process_entry(computed_entry)
        self.E_Alkali = processed_Alkali_entry.energy

        return

    def extract_phase_diagram_info(self,MP_phase_diagram_json_data_filename):

        computed_entries  = self._extract_MP_data(MP_phase_diagram_json_data_filename)
        processed_entries = self.compat.process_entries(computed_entries)

        pd = PhaseDiagram(processed_entries)
        self.phase_diagram_analyser = PDAnalyzer(pd)

        return

    def extract_processed_entries(self,MP_json_data_filename):

        computed_entries  = self._extract_MP_data(MP_json_data_filename)
        processed_entries = self.compat.process_entries(computed_entries)

        return processed_entries

    def extract_energies_above_hull(self,MP_json_data_filename,alkali):

        processed_entries = self.extract_processed_entries(MP_json_data_filename)

        list_energy_above_hull  = []
        list_alkali_content = []

        for entry in processed_entries: 
            decomposition_dict, energy_above_hull  = \
                self.phase_diagram_analyser.get_decomp_and_e_above_hull(entry, allow_negative=True)

            list_energy_above_hull.append(energy_above_hull)  
            list_alkali_content.append(entry.composition[alkali])

        list_energy_above_hull  = np.array(list_energy_above_hull)
        list_alkali_content     = np.array(list_alkali_content )

        return list_alkali_content, list_energy_above_hull  



    def extract_energies(self,MP_json_data_filename,alkali):

        processed_entries = self.extract_processed_entries(MP_json_data_filename)

        list_energy         = []
        list_alkali_content = []
        for entry in processed_entries:
            list_energy.append(entry.energy)
            list_alkali_content.append(entry.composition[alkali])

        list_energy         = np.array(list_energy)
        list_alkali_content = np.array(list_alkali_content )

        I = np.argsort(list_alkali_content )
        
        return list_alkali_content[I], list_energy[I]

    def _extract_MP_data(self,MP_data_filename):

        drone = VaspToComputedEntryDrone()
        queen = BorgQueen(drone, "dummy", 1)

        queen.load_data(MP_data_filename)
        computed_entries = queen.get_data()

        del drone
        del queen

        return computed_entries 

class AnalyseMaterialsProjectJsonDataWithComputedEntries():
    """
    Class which will wrap around boilerplate analysis of MaterialsProject-like
    json files, containing data extracted using borgs and queens.

    It will be assumed that we are providing ComputedEntries objects directly.
    """

    def __init__(self):
        # some MP analysis power tools
        self.compat  = MaterialsProjectCompatibility()

        return

    def extract_alkali_energy(self, computed_Alkali_entry ):
        processed_Alkali_entry = self.compat.process_entry(computed_Alkali_entry)
        self.E_Alkali = processed_Alkali_entry.energy
        return

    def extract_phase_diagram_info(self,MP_phase_diagram_json_data_filename):

        computed_entries  = self._extract_MP_data(MP_phase_diagram_json_data_filename)
        processed_entries = self.compat.process_entries(computed_entries)

        pd = PhaseDiagram(processed_entries)
        self.phase_diagram_analyser = PDAnalyzer(pd)

        return

    def extract_processed_entries(self,computed_entries):
        processed_entries = self.compat.process_entries(computed_entries)

        return processed_entries

    def extract_energies_above_hull(self,computed_entries,alkali):

        processed_entries = self.extract_processed_entries(computed_entries)

        list_energy_above_hull  = []
        list_alkali_content = []

        for entry in processed_entries: 
            decomposition_dict, energy_above_hull  = \
                self.phase_diagram_analyser.get_decomp_and_e_above_hull(entry, allow_negative=True)

            list_energy_above_hull.append(energy_above_hull)  
            list_alkali_content.append(entry.composition[alkali])

        list_energy_above_hull  = np.array(list_energy_above_hull)
        list_alkali_content     = np.array(list_alkali_content )

        return list_alkali_content, list_energy_above_hull  

    def extract_energies(self,computed_entries,alkali):

        processed_entries = self.extract_processed_entries(computed_entries)

        list_energy         = []
        list_alkali_content = []
        for entry in processed_entries:
            list_energy.append(entry.energy)
            list_alkali_content.append(entry.composition[alkali])

        list_energy         = np.array(list_energy)
        list_alkali_content = np.array(list_alkali_content )

        I = np.argsort(list_alkali_content )
        
        return list_alkali_content[I], list_energy[I]

    def _extract_MP_data(self,MP_data_filename):

        drone = VaspToComputedEntryDrone()
        queen = BorgQueen(drone, "dummy", 1)

        queen.load_data(MP_data_filename)
        computed_entries = queen.get_data()

        del drone
        del queen

        return computed_entries 

class AnalyseMaterialsProjectJsonData_NoProcess(AnalyseMaterialsProjectJsonData):
    """
    The Materials Project processing facilities will normalize energies as long as
    certain criteria are met. If not, no data is returned, breaking my voltage
    computation code for slightly forced calculations (putting a U on Fe in a non-oxide
    environment, for example). 
    
    This class will simply avoid the problem by not asking MP to renormalize the energies.
    """

    def extract_processed_entries(self,MP_json_data_filename):

        computed_entries  = self._extract_MP_data(MP_json_data_filename)
        # don't process!
        #processed_entries = self.compat.process_entries(computed_entries)

        return computed_entries 

class AnalyseDensityMatrix(object):
    """ Class to parse the density matrix out of the OUTCAR file (if present) """


    def __init__(self,outcar_file_path):

        self.outcar_file_path = outcar_file_path


    def parse_outcar(self):

        # find the last Iteration in the file
        pattern = 'Iteration'  
        with open(self.outcar_file_path,'r') as file:
            for line in file:
                if re.search(pattern, line):
                    last_interation = line

        print last_interation 

        # Parse the last iteration block
        save_to_block = False
        block = []
        with open(self.outcar_file_path,'r') as file:
            for line in file:
                if line == last_interation: 
                    save_to_block = True

                elif '----------------' in line: 
                    save_to_block = False

                if save_to_block: 
                    #block.append(line.rstrip())
                    block.append(line)


        list_density_matrices = self.parse_block(block)

        return list_density_matrices 


    def parse_block(self,block):

        list_density_matrices = []

        for il, line in enumerate(block):
            if 'atom =' in line:  
                l = int(line.strip().split()[-1])
                if l == 2:
                    sub_block_up = block[il+6:il+11]
                    dm_up = self.parse_sub_block(sub_block_up)

                    sub_block_dn = block[il+14:il+19]
                    dm_dn = self.parse_sub_block(sub_block_dn)
                    
                    list_density_matrices.append([dm_up,dm_dn])


        return list_density_matrices

    def parse_sub_block(self,sub_block):

        density_matrix = []
        for line in sub_block:
            density_matrix.append(map(lambda str:float(str), line.split())[:5])

        return np.array(density_matrix)
            

        return
