### Dengue Model Pipeline

1. main.py runs the entire pipeline.
2. case data requires the following columns:  
        "metadata.primaryDate",
        "location.admin.hierarchy",
        "location.admin1.ID",
        "location.admin2.ID",
        "location.admin3.ID",
        "location.admin5.ID",
        "metadata.diseaseName"

metadata.primaryDate - YYYY-MM-DD format
location.admin1 - state
location.admin2 - district
location.admin3 - subdistrict
location.admin5 - village (not reqd)
metadata.diseaseName - dengue 

all the location columns are location.ID's - which are LGD id's in the format district_529 or state_29 (i.e. regiontype_lgdcode)

geojson's for corresponding regions should also be named similarly i.e. district_529.geojson for eg 
