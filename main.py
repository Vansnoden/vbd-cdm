#-*-coding:utf8-*-

from utilities.library import load_yaml_config


if __name__ == '__main__':

    data = load_yaml_config(fp="./base_config.yaml")
    print(data)

    # 1. load the configurations
    #   call load_yaml_config(path of the configuration file)
    # 2. call data ingestion pipeline 
    #   call ingest_data(list of data sources)
    # 3. if summarization added in yaml
    #   call database_summary()
    # 4. if analysis addes in yaml
    #   call analyze_data(list of pipelines to execute) 






