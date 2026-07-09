
import pandas as pd

def convert_worldbank_data(df):
    data = df.to_dict(orient="records")
    new_data = []
    years = [
        1960,1961,1962,1963,1964,1965,1966,1967,1968,1969,
        1970,1971,1972,1973,1974,1975,1976,1977,1978,1979,
        1980,1981,1982,1983,1984,1985,1986,1987,1988,1989,
        1990,1991,1992,1993,1994,1995,1996,1997,1998,1999,
        2000,2001,2002,2003,2004,2005,2006,2007,2008,2009,
        2010,2011,2012,2013,2014,2015,2016,2017,2018,2019,
        2020,2021,2022,2023,2024,2025
    ]

    for v in data:
        rec = {
            "Country Name": v["Country Name"],
            "Country Code": v["Country Code"],
            "Indicator Name": v["Indicator Name"],
            "Indicator Code": v["Indicator Code"],
            "Year": 0,
            "Value": 0
        }
        for year in years:
            new_data_row = {
                "Country Name": rec["Country Name"],
                "Country Code": rec["Country Code"],
                "Indicator Name": rec["Indicator Name"],
                "Indicator Code": rec["Indicator Code"],
                "Year": year,
                "Value": v[str(year)] if v[str(year)] else 0.0
            }
            new_data.append(new_data_row)
    res = pd.DataFrame.from_dict(new_data, orient='columns')
    res = res.fillna(0, inplace=True)
    print(res[:10])
    return res


if __name__ == '__main__':
    df = convert_worldbank_data(pd.read_csv("/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/siaya_county_data/socio_economics/Clean_API_KEN_DS2_en_csv_v2_10008.csv"))
    df.to_csv("/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/siaya_county_data/socio_economics/Clean_Reorganized_API_KEN_DS2_en_csv_v2_10008.csv")