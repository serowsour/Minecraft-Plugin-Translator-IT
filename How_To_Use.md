------------------------------------------------------------
 HOW TO USE
------------------------------------------------------------
This script translates YAML files while automatically repairing
common formatting issues. It preserves placeholders, Minecraft terms, and structural integrity.

Requirements:
    
     pip install pyyaml googletrans==4.0.0-rc1 deep-translator

Basic usage:
 
     python translate_serowsour.py -i input.yml -l it

 Optional arguments:
 
   -o FILE       Specify a custom output file
   
   -v            Enable verbose logging
   
   -nobackup     Disable automatic backup creation

 Example:
        python translate_serowsour.py -i en_US.yml -l it

 Output:
   Generates a translated YAML file named:
   
     en_US_it.yml

 Processing pipeline:

   ●PHASE A — Pre-processing (validation and repair)
   
   ●PHASE B — Translation
   
   ●PHASE C — Partial post-processing
   
   ●PHASE D — Final cleanup
   
   ●PHASE E — Integrity check



 Completion message:
   Operation completed.

 ------------------------------------------------------------
