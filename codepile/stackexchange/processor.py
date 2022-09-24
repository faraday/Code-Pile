# Only selected tables are extraced from the zip files
# Convert xml to json/parquet and stores them in temp_dir
# Column names are modified to remove "@" at the beginning for convenience
# Transforms "Tags" column of posts table into a list
# Uses pandas "merge", "groupby" and "apply" features do one-one and one-many joins. one-many relationship leads to a nested list of dicts.
# By default unzipping, xml conversion and denormaliztion steps are skipped if the target files are present


import xmltodict
import simplejson
import os
from pathlib import Path
import pandas as pd
import re
from tqdm import tqdm
import py7zr

from codepile.dataset import Processor

class StackExchangeProcessor(Processor):
    def __init__(self, dump_src_dir, temp_dir):
        self.dump_src_dir = dump_src_dir # directory where the downloaded zip files are present for all the sites
        self.temp_dir = temp_dir
        self.output_dir = temp_dir

        self.exclude_sites = [] # exclude list has precedence over include list
        self.include_sites = ["devops.stackexchange.com", "substrate.stackexchange.com"] # "superuser.com", "askubuntu.com"
        self.tables_to_consider = ["Posts", "Comments", "Users"]
        self.intermediate_format = "parquet" # parquet or json

        self.include_columns = {
            "Posts": ["Id", "PostTypeId", "Body", "OwnerUserId", "ParentId"],
            "Users": ["Id", "Reputation", "DisplayName", "AccountId"],
            "Comments": ["Id", "PostId", "Text", "UserId"]
        }
        self.prepare_directories()

    def process(self, force_unzip=False, force_process=False):
        print(f"Processing tables: {self.tables_to_consider}")
        sites_to_process = set()
        for zip_file in os.listdir(self.dump_src_dir):
            if not zip_file.endswith(".7z"):
                continue
            site = self.site_name_from_zipfile(zip_file)
            if self.skip_site(site):
                continue
            
            src_abs_path = os.path.join(self.dump_src_dir, zip_file)
            try:
                self.extract_dump(src_abs_path, site, force_unzip)
                sites_to_process.add(site)
            except Exception as e:
                print(f"Failed to extract compressed file '{zip_file}'")
            
        print(f"Processing {len(sites_to_process)} site/s")
        for site in sites_to_process:
            try:
                print(f"Processing site: {site}")
                self.convert_xml(site)
                self.denormalize_data(site, force_process)
            except Exception as e:
                print(f"Failed to process the site: '{site}'")
                print(e)


    def prepare_directories(self):
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        xml_temp_dir = os.path.join(self.temp_dir, "xml")
        os.makedirs(xml_temp_dir, exist_ok=True)
        parquet_temp_dir = os.path.join(self.temp_dir, self.intermediate_format)
        os.makedirs(parquet_temp_dir, exist_ok=True)    

    def get_site_intermediate_dir(self, site):
        return os.path.join(self.temp_dir, self.intermediate_format, site)

    def get_site_xml_dir(self, site):
        return os.path.join(self.temp_dir, "xml", site)        

    def site_name_from_zipfile(self, zip_file):
        # downloaded zip files for stackoverflow.com are separate for each table/entity.
        if zip_file.startswith("stackoverflow.com-"):
            return "stackoverflow.com"
        else:
            return Path(zip_file).stem

    def skip_site(self, site):
        return (site in self.exclude_sites) or (len(self.include_sites) > 0 and site not in self.include_sites)

    def extract_dump(self, zip_file, site, force_unzip):
        dest_dir = self.get_site_xml_dir(site)

        os.makedirs(dest_dir, exist_ok=True)

        if site == "stackoverflow.com":
            table = Path(zip_file).stem.replace("stackoverflow.com-", "")
            if table in self.tables_to_consider:
                file_names = [table + ".xml"]
            else:
                file_names = []
        else:
            file_names = list(map(lambda x: x + ".xml", self.tables_to_consider))
        
        files_to_extract = set()

        for fn in file_names:
            if not os.path.exists(os.path.join(dest_dir, fn)) or force_unzip:
                files_to_extract.add(fn)
    
        if len(files_to_extract) > 0:
            with py7zr.SevenZipFile(zip_file, 'r') as archive:
                archive.extract(path=dest_dir, targets=list(files_to_extract))

    def convert_xml(self, site):
        xml_src_dir = self.get_site_xml_dir(site)
        dest_dir = self.get_site_intermediate_dir(site)
        format = self.intermediate_format
        os.makedirs(dest_dir, exist_ok=True)
        for table in self.tables_to_consider:
            file_name = table + ".xml"
            src_abs_path = os.path.join(xml_src_dir, file_name)
            target_file = os.path.join(dest_dir, table + "." + format)
            if os.path.exists(target_file):
                print(f"file '{table}' is already converted from xml")
                continue
            with open(src_abs_path, 'r') as xml_file:
                data_dict = xmltodict.parse(xml_file.read())
                if format == "parquet":
                    df = pd.DataFrame.from_dict(data_dict[table.lower()]['row'])
                    df.rename(columns=lambda x: re.sub('@','',x), inplace=True)
                    if table == "Posts":
                        self.transform_tags_column(df)
                    df.to_parquet(target_file)
                elif format == "json":
                    with open(target_file, 'w') as json_file_o:
                        json_file_o.write(simplejson.dumps(data_dict[table.lower()]['row'], ignore_nan=True))
                else:
                    raise ValueError(f"Unsupported target format type: {self.intermediate_format}. supported values are 'parquet', 'json'")
                print(f"Finished converting {table} from xml to {format}")

    def load_data_into_pandas(self, src_dir, tables, format="parquet"):
        dfs = {}
        for table in tables:
            table_path = os.path.join(src_dir, table + "." + format)
            if format == "parquet":
                df = pd.read_parquet(table_path, columns=self.include_columns[table])
            elif format == "json":
                df = pd.read_json(table_path)
                df.rename(columns=lambda x: re.sub('@','',x), inplace=True) # TODO: this renaming could happen during xml to json conversion itself.
            else:
                raise ValueError("Unsuported format")
            dfs[table] = df
        return dfs

    def transform_tags_column(self, df):
        df['Tags'] = df['Tags'].str.replace('><',',').str.replace('<','').str.replace('>','').str.split(',')

    def get_questions_subset(self, df):
        questions_df = df[df['PostTypeId'] == "1"]
        return questions_df

    def denormalize_data(self, site, force_process):
        site_temp_dir = self.get_site_intermediate_dir(site)
        
        output_site_dir = os.path.join(self.output_dir, site)
        os.makedirs(output_site_dir, exist_ok=True)        
        if self.intermediate_format == "parquet":
            output_file = os.path.join(output_site_dir, "questions.parquet")
        else:
            output_file = os.path.join(output_site_dir, "questions.json")

        if os.path.exists(output_file) and not force_process:
            print(f"Skipping, site '{site}' already processed")
            return


        dfs = self.load_data_into_pandas(site_temp_dir, self.tables_to_consider)
        posts_columns = self.include_columns["Posts"]
        comments_columns = self.include_columns["Comments"]
        users_columns = self.include_columns["Users"]
        posts_df = dfs['Posts'][posts_columns]
        posts_df = posts_df[(posts_df.PostTypeId == "1") | (posts_df.PostTypeId == "2")]
        comments_df = dfs['Comments'][comments_columns]
        users_df = dfs['Users'][users_columns]
        users_df = users_df.add_prefix("user_")
        tqdm.pandas()

        # join user info with posts
        print("Adding user info to posts")    
        posts_df = pd.merge(posts_df, users_df, left_on='OwnerUserId', right_on='user_Id', how='left').progress_apply(lambda x: x).drop('user_Id', axis=1)
        # join user info with comments
        print("Adding user info to comments")
        comments_df = pd.merge(comments_df, users_df, left_on='UserId', right_on='user_Id', how='left').progress_apply(lambda x: x).drop('user_Id', axis=1)

        # group comments by posts and populate a list of dictionaries
        print("Grouping comments")
        comments_grouped = comments_df.groupby('PostId').progress_apply(lambda x: x.to_dict('records')).to_frame("comments")

        # populate posts with comments
        print("Adding comments to posts")
        posts_df = pd.merge(posts_df, comments_grouped, left_on="Id", right_on="PostId", how='left').progress_apply(lambda x: x)
        

        questions_df = self.get_questions_subset(posts_df)
        print(f"Found {questions_df.shape[0]} questions")

        # group answers by questions
        print("Grouping answers by questions")
        answers_grouped = posts_df[posts_df.PostTypeId == "2"].groupby("ParentId").progress_apply(lambda x: x.to_dict('records')).to_frame("answers")
        # populate questions with answers 
        print("Adding answers to questions")
        questions_df = pd.merge(questions_df, answers_grouped, left_on='Id', right_on='ParentId', how='left').drop('ParentId', axis=1).progress_apply(lambda x: x)

        if self.intermediate_format == "parquet":
            questions_df.to_parquet(output_file)
        else:
            questions_df.to_json(output_file)
        print(f"Finished processing site: '{site}'")