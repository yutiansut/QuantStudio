# coding=utf-8
import os
import datetime as dt

import numpy as np
import pandas as pd
from traits.api import Str, Bool, Float, Function, Either, List, Enum, on_trait_change

from QuantStudio import __QS_Error__
from QuantStudio.Tools.DateTimeFun import getDateTimeSeries
from QuantStudio.Tools.DataPreprocessingFun import fillNaByLookback
from QuantStudio.Tools.SQLDBFun import genSQLInCondition
from QuantStudio.FactorDataBase.FactorDB import FactorTable

# 将信息源文件中的表和字段信息导入信息文件
def importInfo(info_file, info_resource):
    TableInfo = pd.read_excel(info_resource, "TableInfo").set_index(["TableName"])
    FactorInfo = pd.read_excel(info_resource, 'FactorInfo').set_index(['TableName', 'FieldName'])
    try:
        from QuantStudio.Tools.DataTypeFun import writeNestedDict2HDF5
        writeNestedDict2HDF5(TableInfo, info_file, "/TableInfo")
        writeNestedDict2HDF5(FactorInfo, info_file, "/FactorInfo")
    except:
        pass
    return (TableInfo, FactorInfo)

# 更新信息文件
def updateInfo(info_file, info_resource, logger):
    if not os.path.isfile(info_file):
        logger.warning("数据库信息文件: '%s' 缺失, 尝试从 '%s' 中导入信息." % (info_file, info_resource))
    elif (os.path.getmtime(info_resource)>os.path.getmtime(info_file)):
        logger.warning("数据库信息文件: '%s' 有更新, 尝试从中导入新信息." % info_resource)
    else:
        try:
            from QuantStudio.Tools.DataTypeFun import readNestedDictFromHDF5
            return (readNestedDictFromHDF5(info_file, ref="/TableInfo"), readNestedDictFromHDF5(info_file, ref="/FactorInfo"))
        except:
            logger.warning("数据库信息文件: '%s' 损坏, 尝试从 '%s' 中导入信息." % (info_file, info_resource))
    if not os.path.isfile(info_resource): raise __QS_Error__("缺失数据库信息源文件: %s" % info_resource)
    return importInfo(info_file, info_resource)

def adjustDateTime(data, dts, fillna=False, **kwargs):
    if isinstance(data, (pd.DataFrame, pd.Series)):
        if data.shape[0]==0:
            if isinstance(data, pd.DataFrame): data = pd.DataFrame(index=dts, columns=data.columns)
            else: data = pd.Series(index=dts)
        else:
            if fillna:
                AllDTs = data.index.union(dts)
                AllDTs = AllDTs.sort_values()
                data = data.loc[AllDTs]
                data = data.fillna(**kwargs)
            data = data.loc[dts]
    else:
        if data.shape[1]==0:
            data = pd.Panel(items=data.items, major_axis=dts, minor_axis=data.minor_axis)
        else:
            FactorNames = data.items
            if fillna:
                AllDTs = data.major_axis.union(dts)
                AllDTs = AllDTs.sort_values()
                data = data.loc[:, AllDTs, :]
                data = pd.Panel({data.items[i]:data.iloc[i].fillna(axis=0, **kwargs) for i in range(data.shape[0])})
            data = data.loc[FactorNames, dts, :]
    return data

def adjustDataDTID(data, look_back, factor_names, ids, dts, only_start_lookback=False, only_lookback_nontarget=False, only_lookback_dt=False, logger=None):
    if look_back==0:
        try:
            return data.loc[:, dts, ids]
        except KeyError as e:
            if logger is not None:
                logger.warning("待提取的因子 %s 数据超出了原始数据的时点或 ID 范围, 将填充缺失值!" % (str(list(data.items)), ))
            return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
    AllDTs = data.major_axis.union(dts).sort_values()
    AdjData = data.loc[:, AllDTs, ids]
    if only_start_lookback:# 只在起始时点回溯填充缺失
        AllAdjData = AdjData
        AdjData = AllAdjData.loc[:, :dts[0], :]
        TargetDTs = dts[:1]
    else:
        TargetDTs = dts
    if only_lookback_dt:
        TargetDTs = sorted(set(TargetDTs).difference(data.major_axis))
    if TargetDTs:
        Limits = look_back*24.0*3600
        if only_lookback_nontarget:# 只用非目标时间序列的数据回溯填充
            Mask = pd.Series(np.full(shape=(AdjData.shape[1], ), fill_value=False, dtype=np.bool), index=AdjData.major_axis)
            Mask[TargetDTs] = True
            FillMask = Mask.copy()
            FillMask[Mask.astype("int").diff()!=1] = False
            TimeDelta = pd.Series(np.r_[0, np.diff(Mask.index.values) / np.timedelta64(1, "D")], index=Mask.index)
            TimeDelta[(Mask & (~FillMask)) | (Mask.astype("int").diff()==-1)] = 0
            TimeDelta = TimeDelta.cumsum().loc[TargetDTs]
            FirstDelta = TimeDelta.iloc[0]
            TimeDelta = TimeDelta.diff().fillna(value=0)
            TimeDelta.iloc[0] = FirstDelta
            NewLimits = np.minimum(TimeDelta.values*24.0*3600, Limits).reshape((TimeDelta.shape[0], 1)).repeat(AdjData.shape[2], axis=1)
            Limits = pd.DataFrame(0, index=AdjData.major_axis, columns=AdjData.minor_axis)
            Limits.loc[TargetDTs, :] = NewLimits
        if only_lookback_dt:
            Mask = pd.Series(np.full(shape=(AdjData.shape[1], ), fill_value=False, dtype=np.bool), index=AdjData.major_axis)
            Mask[TargetDTs] = True
            FillMask = Mask.copy()
            FillMask[Mask.astype("int").diff()!=1] = False
            FillMask = FillMask.loc[TargetDTs]
            TimeDelta = pd.Series(np.r_[0, np.diff(Mask.index.values) / np.timedelta64(1, "D")], index=Mask.index).loc[TargetDTs]
            NewLimits = TimeDelta.cumsum().loc[TargetDTs]
            Temp = NewLimits.copy()
            Temp[~FillMask] = np.nan
            Temp = Temp.fillna(method="pad")
            TimeDelta[~FillMask] = np.nan
            NewLimits = NewLimits - Temp + TimeDelta.fillna(method="pad")
            if isinstance(Limits, pd.DataFrame):
                Limits.loc[TargetDTs, :] = np.minimum(NewLimits.values.reshape((NewLimits.shape[0], 1)).repeat(AdjData.shape[2], axis=1), Limits.loc[TargetDTs].values)
            else:
                NewLimits = np.minimum(NewLimits.values*24.0*3600, Limits).reshape((NewLimits.shape[0], 1)).repeat(AdjData.shape[2], axis=1)
                Limits = pd.DataFrame(0, index=AdjData.major_axis, columns=AdjData.minor_axis)
                Limits.loc[TargetDTs, :] = NewLimits
        if np.isinf(look_back) and (not only_lookback_nontarget) and (not only_lookback_dt):
            for i, iFactorName in enumerate(AdjData.items): AdjData.iloc[i].fillna(method="pad", inplace=True)
        else:
            AdjData = dict(AdjData)
            for iFactorName in AdjData: AdjData[iFactorName] = fillNaByLookback(AdjData[iFactorName], lookback=Limits)
            AdjData = pd.Panel(AdjData).loc[factor_names]
    if only_start_lookback:
        AllAdjData.loc[:, dts[0], :] = AdjData.loc[:, dts[0], :]
        return AllAdjData.loc[:, dts]
    else:
        return AdjData.loc[:, dts]


# 基于 SQL 数据库表的因子表
# table_info: Series(index=["DBTableName"]), 可选的 index=["MainTableName", "MainTableID", "JoinCondition", "MainTableCondition", "DefaultSuffix", "Exchange", "SecurityCategory"]
# factor_info: DataFrame(index=[], columns=["DBFieldName", "DataType", "FieldType", "Supplementary", "Description"]), 可选的 columns=["RelatedSQL"]
# security_info: DataFrame(index=[], columns=["Suffix"])
# exchange_info: DataFrame(index=[], columns=["Suffix"])
# 参数编号分配:
# 0 - 100: 因子表特定参数
# 100 - 199: 条件参数, 100: 通用筛选条件
# 200 - 299: 通用参数
class SQL_Table(FactorTable):
    """SQL 因子表"""
    FilterCondition = Str("", arg_type="Dict", label="筛选条件", order=100)
    TableType = Str("", arg_type="SingleOption", label="因子表类型", order=200)
    PreFilterID = Bool(True, arg_type="Bool", label="预筛选ID", order=201)
    #DTField = Enum(None, arg_type="SingleOption", label="时点字段", order=202)
    def __init__(self, name, fdb, sys_args={}, table_prefix="", table_info=None, factor_info=None, security_info=None, exchange_info=None, **kwargs):
        self._TablePrefix = table_prefix
        self._TableInfo = table_info
        self._FactorInfo = factor_info
        self._SecurityInfo = security_info
        self._ExchangeInfo = exchange_info
        self._QS_IgnoredGroupArgs = ("遍历模式", )
        self._DTFormat = "'%Y-%m-%d'"
        self._DTFormat_WithTime = "'%Y-%m-%d %H:%M:%S'"
        self._DBTableName = self._TablePrefix + str(self._TableInfo.loc["DBTableName"])
        super().__init__(name=name, fdb=fdb, sys_args=sys_args, **kwargs)
        # 解析 ID 字段, 至多一个 ID 字段
        self._IDField = self._FactorInfo["DBFieldName"][self._FactorInfo["FieldType"]=="ID"]# ID 字段
        if self._IDField.shape[0]==0:
            self._IDField = None
            self._IDFieldIsStr = True
        else:
            self._IDFieldIsStr = (self.__QS_identifyDataType__(self._FactorInfo["DataType"].loc[self._IDField.index[0]])!="double")
            self._IDField = self._IDField.iloc[0]
        # 解析主表
        self._MainTableName = self._TableInfo.get("MainTableName", None)
        if pd.isnull(self._MainTableName):
            self._MainTableName = self._DBTableName
            self._MainTableID = self._IDField
            self._MainTableCondition = None
        else:
            self._MainTableName = self._TablePrefix + self._MainTableName
            self._MainTableID = self._TableInfo.loc["MainTableID"]
            self._JoinCondition = self._TableInfo.loc["JoinCondition"].format(DBTable=self._DBTableName, MainTable=self._MainTableName)
            self._MainTableCondition = self._TableInfo.loc["MainTableCondition"]
            if pd.notnull(self._MainTableCondition):
                self._MainTableCondition = self._MainTableCondition.format(MainTable=self._MainTableName)
            self._IDFieldIsStr = True# TODO
    def __QS_initArgs__(self):
        super().__QS_initArgs__()
        # 解析时点字段
        Mask = self._FactorInfo["FieldType"].str.lower().str.contains("date")
        Fields = self._FactorInfo[Mask].index.tolist()# 所有的时点字段列表
        if not Fields: Fields = [None]
        self.add_trait("DTField", Enum(*Fields, arg_type="SingleOption", label="时点字段", order=203))
        iFactorInfo = self._FactorInfo[Mask & (self._FactorInfo["Supplementary"]=="Default")]
        if iFactorInfo.shape[0]>0: self.DTField = iFactorInfo.index[0]
        else: self.DTField = Fields[0]
        # 解析条件字段
        self._ConditionFields = self._FactorInfo[self._FactorInfo["FieldType"]=="Condition"].index.tolist()
        for i, iCondition in enumerate(self._ConditionFields):
            self.add_trait("Condition"+str(i), Str("", arg_type="String", label=iCondition, order=i+101))
            iConditionVal = self._FactorInfo.loc[iCondition, "Supplementary"]
            if pd.isnull(iConditionVal) or (isinstance(iConditionVal, str) and (iConditionVal.lower() in ("", "nan"))):
                self[iCondition] = ""
            else:
                self[iCondition] = str(iConditionVal).strip()
    def __QS_genGroupInfo__(self, factors, operation_mode):
        ConditionGroup = {}
        for iFactor in factors:
            iConditions = ";".join([iArgName+":"+str(iFactor[iArgName]) for iArgName in iFactor.ArgNames if iArgName not in self._QS_IgnoredGroupArgs])
            if iConditions not in ConditionGroup:
                ConditionGroup[iConditions] = {"FactorNames":[iFactor.Name], 
                                                       "RawFactorNames":{iFactor._NameInFT}, 
                                                       "StartDT":operation_mode._FactorStartDT[iFactor.Name], 
                                                       "args":iFactor.Args.copy()}
            else:
                ConditionGroup[iConditions]["FactorNames"].append(iFactor.Name)
                ConditionGroup[iConditions]["RawFactorNames"].add(iFactor._NameInFT)
                ConditionGroup[iConditions]["StartDT"] = min(operation_mode._FactorStartDT[iFactor.Name], ConditionGroup[iConditions]["StartDT"])
                if "回溯天数" in ConditionGroup[iConditions]["args"]:
                    ConditionGroup[iConditions]["args"]["回溯天数"] = max(ConditionGroup[iConditions]["args"]["回溯天数"], iFactor.LookBack)
        EndInd = operation_mode.DTRuler.index(operation_mode.DateTimes[-1])
        Groups = []
        for iConditions in ConditionGroup:
            StartInd = operation_mode.DTRuler.index(ConditionGroup[iConditions]["StartDT"])
            Groups.append((self, ConditionGroup[iConditions]["FactorNames"], list(ConditionGroup[iConditions]["RawFactorNames"]), operation_mode.DTRuler[StartInd:EndInd+1], ConditionGroup[iConditions]["args"]))
        return Groups
    def __QS_identifyDataType__(self, field_data_type):
        field_data_type = field_data_type.lower()
        if (field_data_type.find("num")!=-1) or (field_data_type.find("int")!=-1) or (field_data_type.find("decimal")!=-1) or (field_data_type.find("double")!=-1) or (field_data_type.find("float")!=-1):
            return "double"
        elif field_data_type.find("date")!=-1:
            return "object"
        else:
            return "string"
    def __QS_adjustID__(self, ids):
        return ids
    def __QS_restoreID__(self, ids):
        return ids
    def _genIDSQLStr(self, ids, init_keyword="AND", args={}):
        if (ids is not None) and args.get("预筛选ID", self.PreFilterID):
            SQLStr = init_keyword+" ("+genSQLInCondition(self._MainTableName+"."+self._MainTableID, self.__QS_adjustID__(ids), is_str=self._IDFieldIsStr, max_num=1000)+")"
        else:
            SQLStr = init_keyword+" "+self._MainTableName+"."+self._MainTableID+" IS NOT NULL"
        return SQLStr
    def _genFromSQLStr(self, setable_join_str=[]):
        SQLStr = "FROM "+self._DBTableName+" "
        for iJoinStr in setable_join_str: SQLStr += iJoinStr+" "
        if self._DBTableName!=self._MainTableName:
            SQLStr += "INNER JOIN "+self._MainTableName+" "
            SQLStr += "ON "+self._JoinCondition+" "
        return SQLStr[:-1]
    def _getIDField(self, args={}):
        if (self._MainTableName is None) or (self._MainTableName==self._DBTableName):
            RawIDField = self._DBTableName+"."+self._IDField
            if not self._IDFieldIsStr: RawIDField = "CAST("+RawIDField+" AS CHAR)"
        else:
            RawIDField = self._MainTableName+"."+self._MainTableID
        DefaultSuffix = self._TableInfo.get("DefaultSuffix", None)
        Exchange = self._TableInfo.get("Exchange", None)
        SecurityCategory = self._TableInfo.get("SecurityCategory", None)
        Suffix = "{ElseSuffix}"
        if pd.notnull(SecurityCategory):
            SecuCategoryField, SecuCategoryCodes = SecurityCategory.split(":")
            if self._MainTableName is None:
                SecuCategoryField = self._DBTableName + "." + SecuCategoryField
            else:
                SecuCategoryField = self._MainTableName + "." + SecuCategoryField
            SecuCategoryCodes = SecuCategoryCodes.split(",")
            SecurityInfo = self._SecurityInfo
            iSuffix = "CASE "+SecuCategoryField+" "
            for iCode in SecuCategoryCodes:
                iSuffix += "WHEN "+iCode+" THEN '"+SecurityInfo.loc[iCode, "Suffix"]+"' "
            iSuffix += "ELSE {ElseSuffix} END"
            Suffix = Suffix.format(ElseSuffix=iSuffix)
        if pd.notnull(Exchange):
            ExchangeField, ExchangeCodes = Exchange.split(":")
            if self._MainTableName is None:
                ExchangeField = self._DBTableName + "." + ExchangeField
            else:
                ExchangeField = self._MainTableName + "." + ExchangeField
            ExchangeCodes = ExchangeCodes.split(",")
            ExchangeInfo = self._ExchangeInfo
            iSuffix = "CASE "+ExchangeField+" "
            for iCode in ExchangeCodes:
                iSuffix += "WHEN "+iCode+" THEN '"+ExchangeInfo.loc[iCode, "Suffix"]+"' "
            iSuffix += "ELSE {ElseSuffix} END"
            Suffix = Suffix.format(ElseSuffix=iSuffix)
        Suffix = Suffix.format(ElseSuffix=("''" if pd.isnull(DefaultSuffix) else "'"+DefaultSuffix+"'"))
        if Suffix=="''": return RawIDField
        else: return "CONCAT("+RawIDField+", "+Suffix+")"
    def _adjustRawDataByRelatedField(self, raw_data, fields):# TODO JYDB
        if "RelatedSQL" not in self._FactorInfo: return raw_data
        RelatedFields = self._FactorInfo["RelatedSQL"].loc[fields]
        RelatedFields = RelatedFields[pd.notnull(RelatedFields)]
        if RelatedFields.shape[0]==0: return raw_data
        for iField in RelatedFields.index:
            iOldData = raw_data.pop(iField)
            iOldDataType = self.__QS_identifyDataType__(self._FactorInfo.loc[iField[:-2], "DataType"])
            iDataType = self.__QS_identifyDataType__(self._FactorInfo.loc[iField, "DataType"])
            if iDataType=="double":
                iNewData = pd.Series(np.nan, index=raw_data.index, dtype="float")
            else:
                iNewData = pd.Series(np.full(shape=(raw_data.shape[0], ), fill_value=None, dtype="O"), index=raw_data.index, dtype="O")
            iSQLStr = self._FactorInfo.loc[iField, "RelatedSQL"]
            if iSQLStr[0]=="{":
                iMapInfo = eval(iSQLStr).items()
            else:
                iStartIdx = iSQLStr.find("{KeyCondition}")
                if iStartIdx!=-1:
                    iEndIdx = iSQLStr[iStartIdx:].find(" ")
                    if iEndIdx==-1: iEndIdx = len(iSQLStr)
                    else: iEndIdx += iStartIdx
                    iStartIdx += 14
                    KeyField = iSQLStr[iStartIdx:iEndIdx]
                    iKeys = iOldData[pd.notnull(iOldData)].unique().tolist()
                    if iKeys:
                        KeyCondition = genSQLInCondition(KeyField, iKeys, is_str=(iOldDataType!="double"))
                    else:
                        KeyCondition = KeyField+" IN (NULL)"
                    iSQLStr = iSQLStr.replace("{KeyCondition}"+KeyField, "{KeyCondition}")
                else:
                    KeyCondition = ""
                if iSQLStr.find("{Keys}")!=-1:
                    Keys = ", ".join([str(iKey) for iKey in iOldData[pd.notnull(iOldData)].unique()])
                    if not Keys: Keys = "NULL"
                else:
                    Keys = ""
                iMapInfo = self._FactorDB.fetchall(iSQLStr.format(TablePrefix=self._TablePrefix, Keys=Keys, KeyCondition=KeyCondition))
            for jVal, jRelatedVal in iMapInfo:
                if pd.notnull(jVal):
                    if iOldDataType!="double":
                        iNewData[iOldData==str(jVal)] = jRelatedVal
                    elif isinstance(jVal, str):
                        iNewData[iOldData==float(jVal)] = jRelatedVal
                    else:
                        iNewData[iOldData==jVal] = jRelatedVal
                else:
                    iNewData[pd.isnull(iOldData)] = jRelatedVal
            raw_data[iField] = iNewData
        return raw_data
    def _genFieldSQLStr(self, factor_names):# TODO JYDB
        SQLStr = ""
        JoinStr = []
        SETables = set()
        for iField in factor_names:
            iInfo = self._FactorInfo.loc[iField, "Supplementary"]
            if isinstance(iInfo, str) and (iInfo.find("从表")!=-1):
                iInfo = iInfo.split(":")
                iSETable, iJoinField = iInfo[-2:]
                SQLStr += iSETable+"."+self._FactorInfo.loc[iField, "DBFieldName"]+", "
                if iSETable not in SETables:
                    JoinStr.append("LEFT JOIN "+iSETable+" ON "+self._DBTableName+".ID="+iSETable+"."+iJoinField)
                    SETables.add(iSETable)
            else:
                SQLStr += self._DBTableName+"."+self._FactorInfo.loc[iField, "DBFieldName"]+", "
        return (SQLStr[:-2], JoinStr)
    def _genConditionSQLStr(self, use_main_table=True, init_keyword="AND", args={}):
        FilterStr = args.get("筛选条件", self.FilterCondition)
        if FilterStr:
            SQLStr = init_keyword+" "+FilterStr.format(Table=self._DBTableName, TablePrefix=self._TablePrefix)+" "
            init_keyword = "AND"
        else: SQLStr = ""
        for iConditionField in self._ConditionFields:
            iConditionVal = args.get(iConditionField, self[iConditionField])
            if iConditionVal:
                if self.__QS_identifyDataType__(self._FactorInfo.loc[iConditionField, "DataType"])!="double":
                    SQLStr += init_keyword+" "+self._DBTableName+"."+self._FactorInfo.loc[iConditionField, "DBFieldName"]+" IN ('"+"','".join(iConditionVal.split(","))+"') "
                else:
                    SQLStr += init_keyword+" "+self._DBTableName+"."+self._FactorInfo.loc[iConditionField, "DBFieldName"]+" IN ("+iConditionVal+") "
                init_keyword = "AND"
        if use_main_table and pd.notnull(self._MainTableCondition): SQLStr += init_keyword+" "+self._MainTableCondition+" "
        return SQLStr[:-1]
    def getCondition(self, icondition, ids=None, dts=None, args={}):
        SQLStr = "SELECT DISTINCT "+self._DBTableName+"."+self._FactorInfo.loc[icondition, "DBFieldName"]+" "
        SQLStr += self._genFromSQLStr()+" "
        if ids is not None: SQLStr += "WHERE ("+genSQLInCondition(self._MainTableName+"."+self._MainTableID, self.__QS_adjustID__(ids), is_str=self._IDFieldIsStr, max_num=1000)+") "
        else: SQLStr += "WHERE "+self._MainTableName+"."+self._MainTableID+" IS NOT NULL "
        if (dts is not None) and hasattr(self, "DTField"):
            DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
            SQLStr += "AND ("+genSQLInCondition(DTField, [iDT.strftime(self._DTFormat) for iDT in dts], is_str=False, max_num=1000)+") "
        if pd.notnull(self._MainTableCondition): SQLStr += "AND "+self._MainTableCondition+" "
        SQLStr += "ORDER BY "+self._DBTableName+"."+self._FactorInfo.loc[icondition, "DBFieldName"]
        return [iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)]
    def getMetaData(self, key=None, args={}):
        if key is None:
            return self._TableInfo
        else:
            return self._TableInfo.get(key, None)
    @property
    def FactorNames(self):
        return self._FactorInfo[pd.notnull(self._FactorInfo["FieldType"])].index.tolist()
    def getFactorMetaData(self, factor_names=None, key=None, args={}):
        if factor_names is None:
            factor_names = self.FactorNames
        if key=="DataType":
            if hasattr(self, "_DataType"): return self._DataType.loc[factor_names]
            return self._FactorInfo["DataType"].loc[factor_names].apply(self.__QS_identifyDataType__)
        elif key=="Description": return self._FactorInfo["Description"].loc[factor_names]
        elif key is None:
            return pd.DataFrame({"DataType":self.getFactorMetaData(factor_names, key="DataType", args=args),
                                 "Description":self.getFactorMetaData(factor_names, key="Description", args=args)})
        else:
            return pd.Series([None]*len(factor_names), index=factor_names, dtype=np.dtype("O"))


# 基于 SQL 数据库表的宽因子表
# 一个字段标识 ID, 一个字段标识时点, 其余字段为因子
class SQL_WideTable(SQL_Table):
    """SQL 宽因子表"""
    LookBack = Float(0, arg_type="Integer", label="回溯天数", order=0)
    OnlyStartLookBack = Bool(False, label="只起始日回溯", arg_type="Bool", order=1)
    OnlyLookBackNontarget = Bool(False, label="只回溯非目标日", arg_type="Bool", order=2)
    OnlyLookBackDT = Bool(False, label="只回溯时点", arg_type="Bool", order=3)
    #PublDTField = Enum(None, label="公告时点字段", arg_type="SingleOption", order=4)
    IgnoreTime = Bool(True, label="忽略时间", arg_type="Bool", order=5)
    EndDateASC = Bool(False, label="截止日期递增", arg_type="Bool", order=6)
    OrderFields = List(arg_type="List", label="排序字段", order=7)# [("字段名", "ASC" 或者 "DESC")]
    MultiMapping = Bool(True, label="多重映射", arg_type="Bool", order=8)
    Operator = Either(Function(None), None, arg_type="Function", label="算子", order=9)
    OperatorDataType = Enum("object", "double", "string", arg_type="SingleOption", label="算子数据类型", order=10)
    def __init__(self, name, fdb, sys_args={}, table_prefix="", table_info=None, factor_info=None, security_info=None, exchange_info=None, **kwargs):
        super().__init__(name=name, fdb=fdb, sys_args=sys_args, table_prefix=table_prefix, table_info=table_info, factor_info=factor_info, security_info=security_info, exchange_info=exchange_info, **kwargs)
        self._QS_IgnoredGroupArgs = ("遍历模式", "回溯天数", "只起始日回溯", "只回溯非目标日", "只回溯时点")
    def __QS_initArgs__(self):
        super().__QS_initArgs__()
        # 解析公告时点字段
        Fields = self._FactorInfo[self._FactorInfo["FieldType"].str.lower().str.contains("date")].index.tolist()# 所有的时点字段列表
        Fields += [None]
        self.add_trait("PublDTField", Enum(*Fields, arg_type="SingleOption", label="公告时点字段", order=4))
        PublDTField = self._FactorInfo["DBFieldName"][self._FactorInfo["FieldType"]=="AnnDate"]
        if PublDTField.shape[0]==0: self.PublDTField = None
        else: self.PublDTField = PublDTField.index[0]
    def getFactorMetaData(self, factor_names=None, key=None, args={}):
        if key=="DataType":
            if not args.get("多重映射", self.MultiMapping): return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
            if factor_names is None: factor_names = self.FactorNames
            if args.get("算子", self.Operator) is None:
                return pd.Series(["object"]*len(factor_names), index=factor_names)
            else:
                return pd.Series([args.get("算子数据类型", self.OperatorDataType)]*len(factor_names), index=factor_names)
        else:
            return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
    # 返回在给定时点 idt 的有数据记录的 ID
    # 如果 idt 为 None, 将返回所有有历史数据记录的 ID
    # 忽略 ifactor_name
    # 返回在给定时点 idt 的有数据记录的 ID
    # 如果 idt 为 None, 将返回所有有历史数据记录的 ID
    # 忽略 ifactor_name
    def getID(self, ifactor_name=None, idt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT DISTINCT "+self._getIDField(args=args)+" AS ID "
        SQLStr += self._genFromSQLStr()+" "
        if idt is not None: SQLStr += "WHERE "+DTField+"="+idt.strftime(self._DTFormat)+" "
        else: SQLStr += "WHERE "+DTField+" IS NOT NULL "
        SQLStr += "AND "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID"
        return self.__QS_restoreID__([iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)])
    # 返回在给定 ID iid 的有数据记录的时间点
    # 如果 iid 为 None, 将返回所有有历史数据记录的时间点
    # 忽略 ifactor_name
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT DISTINCT "+DTField+" "
        if iid is not None:
            SQLStr += self._genFromSQLStr()+" "
            SQLStr += "WHERE "+self._MainTableName+"."+self._MainTableID+"='"+self.__QS_adjustID__([iid])[0]+"' "
            SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        else:
            SQLStr += "FROM "+self._DBTableName+" "
            SQLStr += "WHERE "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
            SQLStr += self._genConditionSQLStr(use_main_table=False, args=args)+" "
        if start_dt is not None: SQLStr += "AND "+DTField+">="+start_dt.strftime(self._DTFormat)+" "
        if end_dt is not None: SQLStr += "AND "+DTField+"<="+end_dt.strftime(self._DTFormat)+" "
        SQLStr += "ORDER BY "+DTField
        return [iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)]
    def _genNullIDSQLStr_WithPublDT(self, factor_names, ids, end_date, args={}):
        IgnoreTime = args.get("忽略时间", self.IgnoreTime)
        if IgnoreTime: DTFormat = self._DTFormat
        else: DTFormat = self._DTFormat_WithTime
        EndDTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        AnnDTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("公告时点字段", self.PublDTField), "DBFieldName"]
        SubSQLStr = "SELECT "+self._DBTableName+"."+self._IDField+", "
        SubSQLStr += "MAX("+EndDTField+") AS MaxEndDate "
        SubSQLStr += "FROM "+self._DBTableName+" "
        SubSQLStr += "WHERE ("+AnnDTField+"<"+end_date.strftime(DTFormat)+" "
        SubSQLStr += "AND "+EndDTField+"<"+end_date.strftime(DTFormat)+") "
        SubSQLStr += self._genConditionSQLStr(use_main_table=False, args=args)+" "
        if (self._MainTableName is None) or (self._MainTableName==self._DBTableName):
            if args.get("预筛选ID", self.PreFilterID):
                SubSQLStr += "AND ("+genSQLInCondition(self._DBTableName+"."+self._IDField, self.__QS_adjustID__(ids), is_str=self._IDFieldIsStr, max_num=1000)+") "
            else:
                SubSQLStr += "AND "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        SubSQLStr += "GROUP BY "+self._DBTableName+"."+self._IDField
        if IgnoreTime:
            SQLStr = "SELECT DATE(CASE WHEN "+AnnDTField+">=t.MaxEndDate THEN "+AnnDTField+" ELSE t.MaxEndDate END) AS DT, "
        else:
            SQLStr = "SELECT CASE WHEN "+AnnDTField+">=t.MaxEndDate THEN "+AnnDTField+" ELSE t.MaxEndDate END AS DT, "
        SQLStr += self._getIDField(args=args)+" AS ID, "
        SQLStr += "t.MaxEndDate AS MaxEndDate, "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        SQLStr += "INNER JOIN ("+SubSQLStr+") t "
        SQLStr += "ON (t."+self._IDField+"="+self._DBTableName+"."+self._IDField+" "
        SQLStr += "AND "+EndDTField+"=t.MaxEndDate) "
        SQLStr += self._genIDSQLStr(ids, init_keyword="WHERE", args=args)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)
        return SQLStr
    def _prepareRawData_WithPublDT(self, factor_names, ids, dts, args={}):
        if (dts==[]) or (ids==[]): return pd.DataFrame(columns=["QS_DT", "ID"]+factor_names)
        IgnoreTime = args.get("忽略时间", self.IgnoreTime)
        if IgnoreTime: DTFormat = self._DTFormat
        else: DTFormat = self._DTFormat_WithTime
        StartDate, EndDate = dts[0].date(), dts[-1].date()
        LookBack = args.get("回溯天数", self.LookBack)
        if not np.isinf(LookBack): StartDate -= dt.timedelta(LookBack)
        EndDTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        AnnDTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("公告时点字段", self.PublDTField), "DBFieldName"]
        SubSQLStr = "SELECT "+self._DBTableName+"."+self._IDField+", "
        if IgnoreTime:
            SubSQLStr += "DATE(CASE WHEN "+AnnDTField+">="+EndDTField+" THEN "+AnnDTField+" ELSE "+EndDTField+" END) AS AnnDate, "
        else:
            SubSQLStr += "CASE WHEN "+AnnDTField+">="+EndDTField+" THEN "+AnnDTField+" ELSE "+EndDTField+" END AS AnnDate, "
        SubSQLStr += "MAX("+EndDTField+") AS MaxEndDate "
        SubSQLStr += "FROM "+self._DBTableName+" "
        SubSQLStr += "WHERE ("+AnnDTField+">="+StartDate.strftime(DTFormat)+" "
        SubSQLStr += "OR "+EndDTField+">="+StartDate.strftime(DTFormat)+") "
        SubSQLStr += "AND ("+AnnDTField+"<="+EndDate.strftime(DTFormat)+" "
        SubSQLStr += "AND "+EndDTField+"<="+EndDate.strftime(DTFormat)+") "
        SubSQLStr += self._genConditionSQLStr(use_main_table=False, args=args)+" "
        if (self._MainTableName is None) or (self._MainTableName==self._DBTableName):
            if (ids is not None) and args.get("预筛选ID", self.PreFilterID):
                SubSQLStr += "AND ("+genSQLInCondition(self._DBTableName+"."+self._IDField, self.__QS_adjustID__(ids), is_str=self._IDFieldIsStr, max_num=1000)+") "
            else:
                SubSQLStr += "AND "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        if IgnoreTime:
            SubSQLStr += "GROUP BY "+self._DBTableName+"."+self._IDField+", DATE(AnnDate)"
        else:
            SubSQLStr += "GROUP BY "+self._DBTableName+"."+self._IDField+", AnnDate"
        SQLStr = "SELECT t.AnnDate AS DT, "
        SQLStr += self._getIDField(args=args)+" AS ID, "
        SQLStr += "t.MaxEndDate AS MaxEndDate, "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        SQLStr += "INNER JOIN ("+SubSQLStr+") t "
        SQLStr += "ON (t."+self._IDField+"="+self._DBTableName+"."+self._IDField+") "
        SQLStr += "AND (t.MaxEndDate="+EndDTField+") "
        SQLStr += self._genIDSQLStr(ids, init_keyword="WHERE", args=args)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID, DT"
        RawData = self._FactorDB.fetchall(SQLStr)
        if not RawData: RawData = pd.DataFrame(columns=["QS_DT", "ID", "MaxEndDate"]+factor_names)
        else:
            RawData = pd.DataFrame(np.array(RawData, dtype="O"), columns=["QS_DT", "ID", "MaxEndDate"]+factor_names)
            RawData["ID"] = self.__QS_restoreID__(RawData["ID"])
        if np.isinf(LookBack):
            if ids is None: ids = self.getID(args=args)
            NullIDs = set(ids).difference(set(RawData[RawData["QS_DT"]==dt.datetime.combine(StartDate,dt.time(0))]["ID"]))
            if NullIDs:
                NullRawData = self._FactorDB.fetchall(self._genNullIDSQLStr_WithPublDT(factor_names, list(NullIDs), StartDate, args=args))
                if NullRawData:
                    NullRawData = pd.DataFrame(np.array(NullRawData, dtype="O"), columns=["QS_DT", "ID", "MaxEndDate"]+factor_names)
                    NullRawData["ID"] = self.__QS_restoreID__(NullRawData["ID"])
                    RawData = pd.concat([NullRawData, RawData], ignore_index=True)
                    RawData.sort_values(by=["ID", "QS_DT"])
        if RawData.shape[0]==0: return RawData.loc[:, ["QS_DT", "ID"]+factor_names]
        if args.get("截止日期递增", self.EndDateASC):# 删除截止日期非递增的记录
            DTRank = RawData.loc[:, ["ID", "QS_DT", "MaxEndDate"]].set_index(["ID"]).astype(np.datetime64).groupby(axis=0, level=0).rank(method="min")
            RawData = RawData[(DTRank["QS_DT"]<=DTRank["MaxEndDate"]).values]
        RawData = RawData.loc[:, ["QS_DT", "ID"]+factor_names]
        RawData = self._adjustRawDataByRelatedField(RawData, factor_names)
        return RawData
    def _genNullIDSQLStr_IgnorePublDT(self, factor_names, ids, end_date, args={}):
        IDField = self._getIDField(args=args)
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SubSQLStr = "SELECT "+self._MainTableName+"."+self._MainTableID+", "
        SubSQLStr += "MAX("+DTField+") "
        SubSQLStr += self._genFromSQLStr()+" "
        SubSQLStr += "WHERE "+DTField+"<"+end_date.strftime(self._DTFormat)+" "
        SubSQLStr += self._genIDSQLStr(ids, args=args)+" "
        ConditionSQLStr = self._genConditionSQLStr(use_main_table=True, args=args)
        SubSQLStr += ConditionSQLStr+" "
        SubSQLStr += "GROUP BY "+self._MainTableName+"."+self._MainTableID
        SQLStr = "SELECT "+DTField+", "
        SQLStr += IDField+" AS ID, "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        SQLStr += "WHERE ("+self._MainTableName+"."+self._MainTableID+", "+DTField+") IN ("+SubSQLStr+") "
        SQLStr += ConditionSQLStr
        return SQLStr
    def _prepareRawData_IgnorePublDT(self, factor_names, ids, dts, args={}):
        if (dts==[]) or (ids==[]): return pd.DataFrame(columns=["QS_DT", "ID"]+factor_names)
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        if args.get("忽略时间", self.IgnoreTime): DTFormat = self._DTFormat
        else: DTFormat = self._DTFormat_WithTime
        LookBack = args.get("回溯天数", self.LookBack)
        if dts is not None:
            StartDate, EndDate = dts[0].date(), dts[-1].date()
            if not np.isinf(LookBack): StartDate -= dt.timedelta(LookBack)
        else:
            StartDate = EndDate = None
        # 形成 SQL 语句, 时点, ID, 因子数据
        SQLStr = "SELECT "+DTField+", "
        SQLStr += self._getIDField(args=args)+" AS ID, "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        if StartDate is not None:
            SQLStr += "WHERE "+DTField+">="+StartDate.strftime(DTFormat)+" "
            SQLStr += "AND "+DTField+"<="+EndDate.strftime(DTFormat)+" "
        else:
            SQLStr += "WHERE "+DTField+" IS NOT NULL "
        SQLStr += self._genIDSQLStr(ids, args=args)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID, "+DTField
        RawData = self._FactorDB.fetchall(SQLStr)
        if not RawData: RawData = pd.DataFrame(columns=["QS_DT", "ID"]+factor_names)
        else:
            RawData = pd.DataFrame(np.array(RawData), columns=["QS_DT", "ID"]+factor_names)
            RawData["ID"] = self.__QS_restoreID__(RawData["ID"])
        if (StartDate is not None) and np.isinf(LookBack):
            if ids is None: ids = self.getID(args=args)
            NullIDs = set(ids).difference(set(RawData[RawData["QS_DT"]==dt.datetime.combine(StartDate, dt.time(0))]["ID"]))
            if NullIDs:
                NullRawData = self._FactorDB.fetchall(self._genNullIDSQLStr_IgnorePublDT(factor_names, list(NullIDs), StartDate, args=args))
                if NullRawData:
                    NullRawData = pd.DataFrame(np.array(NullRawData, dtype="O"), columns=["QS_DT", "ID"]+factor_names)
                    NullRawData["ID"] = self.__QS_restoreID__(NullRawData["ID"])
                    RawData = pd.concat([NullRawData, RawData], ignore_index=True)
                    RawData.sort_values(by=["ID", "QS_DT"])
        if RawData.shape[0]==0: return RawData
        RawData = self._adjustRawDataByRelatedField(RawData, factor_names)
        return RawData
    def __QS_prepareRawData__(self, factor_names, ids, dts, args={}):
        OrderFields = args.get("排序字段", self.OrderFields)
        if OrderFields:
            OrderFields, Orders = np.array(OrderFields).T.tolist()
        else:
            OrderFields, Orders = [], []
        FactorNames = list(set(factor_names).union(OrderFields))
        if args.get("公告时点字段", self.PublDTField) is None:
            RawData = self._prepareRawData_IgnorePublDT(factor_names=FactorNames, ids=ids, dts=dts, args=args)
        else:
            RawData = self._prepareRawData_WithPublDT(factor_names=FactorNames, ids=ids, dts=dts, args=args)
        RawData = RawData.sort_values(by=["ID", "QS_DT"]+OrderFields, ascending=[True, True]+[(iOrder.lower()=="asc") for iOrder in Orders])
        return RawData.loc[:, ["QS_DT", "ID"]+factor_names]
    def _calcListData(self, raw_data, factor_names, ids, dts, args={}):
        Operator = args.get("算子", self.Operator)
        if Operator is None: Operator = (lambda x: x.tolist())
        if args.get("只回溯时点", self.OnlyLookBackDT):
            DeduplicatedIndex = raw_data.index(~raw_data.index.duplicated())
            RowIdxMask = pd.Series(False, index=DeduplicatedIndex).unstack(fill_value=True).astype(bool)
            RawIDs = RowIdxMask.columns
            if RawIDs.intersection(ids).shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
            RowIdx = pd.DataFrame(np.arange(RowIdxMask.shape[0]).reshape((RowIdxMask.shape[0], 1)).repeat(RowIdxMask.shape[1], axis=1), index=RowIdxMask.index, columns=RawIDs)
            RowIdx[RowIdxMask] = np.nan
            RowIdx = adjustDataDTID(pd.Panel({"RowIdx": RowIdx}), args.get("回溯天数", self.LookBack), ["RowIdx"], RowIdx.columns.tolist(), dts, 
                                              args.get("只起始日回溯", self.OnlyStartLookBack), 
                                              args.get("只回溯非目标日", self.OnlyLookBackNontarget), 
                                              logger=self._QS_Logger).iloc[0].values
            RowIdx[pd.isnull(RowIdx)] = -1
            RowIdx = RowIdx.astype(int)
            ColIdx = np.arange(RowIdx.shape[1]).reshape((1, RowIdx.shape[1])).repeat(RowIdx.shape[0], axis=0)
            RowIdxMask = (RowIdx==-1)
            Data = {}
            for iFactorName in factor_names:
                iRawData = raw_data[iFactorName].groupby(axis=0, level=[0, 1]).apply(Operator).unstack()
                iRawData = iRawData.values[RowIdx, ColIdx]
                iRawData[RowIdxMask] = None
                Data[iFactorName] = pd.DataFrame(iRawData, index=dts, columns=RawIDs)
            return pd.Panel(Data).loc[factor_names, :, ids]
        else:
            Data = {}
            for iFactorName in factor_names:
                Data[iFactorName] = raw_data[iFactorName].groupby(axis=0, level=[0, 1]).apply(Operator).unstack()
            Data = pd.Panel(Data).loc[factor_names]
            return adjustDataDTID(Data, args.get("回溯天数", self.LookBack), factor_names, ids, dts, 
                                  args.get("只起始日回溯", self.OnlyStartLookBack), 
                                  args.get("只回溯非目标日", self.OnlyLookBackNontarget), 
                                  logger=self._QS_Logger)
    def __QS_calcData__(self, raw_data, factor_names, ids, dts, args={}):
        if raw_data.shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
        if ids is None: ids = sorted(raw_data["ID"].unique())
        raw_data = raw_data.set_index(["QS_DT", "ID"])
        MultiMapping = args.get("多重映射", self.MultiMapping)
        if MultiMapping:
            return self._calcListData(raw_data, factor_names, ids, dts, args=args)
        else:
            if not raw_data.index.is_unique:
                raise __QS_Error__("%s 的表 %s 无法保证唯一性, 可以尝试将 '因子值类型' 参数取值调整为 'list'" % (self._FactorDB.Name, self.Name))
        DataType = self.getFactorMetaData(factor_names=factor_names, key="DataType", args=args)
        if args.get("只回溯时点", self.OnlyLookBackDT):
            RowIdxMask = pd.Series(False, index=raw_data.index).unstack(fill_value=True).astype(bool)
            RawIDs = RowIdxMask.columns
            if RawIDs.intersection(ids).shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
            RowIdx = pd.DataFrame(np.arange(RowIdxMask.shape[0]).reshape((RowIdxMask.shape[0], 1)).repeat(RowIdxMask.shape[1], axis=1), index=RowIdxMask.index, columns=RawIDs)
            RowIdx[RowIdxMask] = np.nan
            RowIdx = adjustDataDTID(pd.Panel({"RowIdx": RowIdx}), args.get("回溯天数", self.LookBack), ["RowIdx"], RowIdx.columns.tolist(), dts, 
                                              args.get("只起始日回溯", self.OnlyStartLookBack), 
                                              args.get("只回溯非目标日", self.OnlyLookBackNontarget), 
                                              logger=self._QS_Logger).iloc[0].values
            RowIdx[pd.isnull(RowIdx)] = -1
            RowIdx = RowIdx.astype(int)
            ColIdx = np.arange(RowIdx.shape[1]).reshape((1, RowIdx.shape[1])).repeat(RowIdx.shape[0], axis=0)
            RowIdxMask = (RowIdx==-1)
            Data = {}
            for iFactorName in raw_data.columns:
                iRawData = raw_data[iFactorName].unstack()
                if DataType[iFactorName]=="double":
                    try:
                        iRawData = iRawData.astype("float")
                    except:
                        pass
                iRawData = iRawData.values[RowIdx, ColIdx]
                iRawData[RowIdxMask] = None
                Data[iFactorName] = pd.DataFrame(iRawData, index=dts, columns=RawIDs)
            return pd.Panel(Data).loc[factor_names, :, ids]
        else:
            Data = {}
            for iFactorName in raw_data.columns:
                iRawData = raw_data[iFactorName].unstack()
                if DataType[iFactorName]=="double":
                    try:
                        iRawData = iRawData.astype("float")
                    except:
                        pass
                Data[iFactorName] = iRawData
            Data = pd.Panel(Data).loc[factor_names]
            return adjustDataDTID(Data, args.get("回溯天数", self.LookBack), factor_names, ids, dts, 
                                  args.get("只起始日回溯", self.OnlyStartLookBack), 
                                  args.get("只回溯非目标日", self.OnlyLookBackNontarget), 
                                  logger=self._QS_Logger)

# 基于 SQL 数据库表的窄因子表, TODO
# 一个字段标识 ID, 一个字段标识时点, 一个字段标识因子名(不存在则固定取标识因子值字段的名称作为因子名), 一个字段标识为因子值
class SQL_NarrowTable(SQL_Table):
    """SQL 窄因子表"""
    LookBack = Float(0, arg_type="Integer", label="回溯天数", order=0)
    OnlyStartLookBack = Bool(False, label="只起始日回溯", arg_type="Bool", order=1)
    #FactorNameField = Enum(None, arg_type="SingleOption", label="因子名字段", order=2)
    #FactorValueField = Enum(None, arg_type="SingleOption", label="因子值字段", order=3)
    def __init__(self, name, fdb, sys_args={}, table_prefix="", table_info=None, factor_info=None, security_info=None, exchange_info=None, **kwargs):
        super().__init__(name=name, fdb=fdb, sys_args=sys_args, table_prefix=table_prefix, table_info=table_info, factor_info=factor_info, security_info=security_info, exchange_info=exchange_info, **kwargs)
        self._FactorNames = None# 所有的因子名列表或者对照字典
        self._QS_IgnoredGroupArgs = ("遍历模式", "回溯天数", "只起始日回溯")
    def __QS_initArgs__(self):
        super().__QS_initArgs__()
        FactorFields = self._FactorInfo[self._FactorInfo["FieldType"]=="Factor"]
        self.add_trait("FactorNameField", Enum(*FactorFields.index.tolist(), arg_type="SingleOption", label="因子名字段", order=2))
        DefaultField = FactorFields[FactorFields["Supplementary"]=="Default"].index
        if DefaultField.shape[0]==0: self.FactorField = FactorFields.index[0]
        else: self.FactorField = DefaultField[0]
        ValueFields = self._FactorInfo[self._FactorInfo["FieldType"]=="Value"]
        self.add_trait("FactorValueField", Enum(*ValueFields.index.tolist(), arg_type="SingleOption", label="因子值字段", order=3))
        DefaultField = ValueFields[ValueFields["Supplementary"]=="Default"].index
        if DefaultField.shape[0]==0: self.FactorValueField = ValueFields.index[0]
        else: self.FactorValueField = DefaultField[0]
    @on_trait_change("FactorNameField")
    def _on_FactorNameField_changed(self, obj, name, old, new):
        if self.FactorNameField is not None: self._FactorNames = None
    def _getFactorNames(self, factor_field, check_list=False):
        if (factor_field==self.FactorNameField) and (self._FactorNames is not None): return self._FactorNames
        FactorField = self._DBTableName+"."+self._FactorInfo.loc[factor_field, "DBFieldName"]
        if "RelatedSQL" in self._FactorInfo: SQLStr = self._FactorInfo.loc[factor_field, "RelatedSQL"]
        else: SQLStr = None
        if pd.isnull(SQLStr) or (not SQLStr):
            if check_list: return []
            SQLStr = "SELECT DISTINCT "+FactorField+" FROM "+self._DBTableName+" WHERE "+FactorField+" IS NOT NULL ORDER BY "+FactorField
            FactorNames = [str(iName) for iName, in self._FactorDB.fetchall(SQLStr)]
        else:
            SubSQLStr = "SELECT DISTINCT "+FactorField+" FROM "+self._DBTableName+" WHERE "+FactorField+" IS NOT NULL"
            SQLStr = SQLStr.format(Keys=SubSQLStr)
            FactorNames = {iName:iCode for iCode, iName in self._FactorDB.fetchall(SQLStr)}
        if factor_field==self.FactorNameField: self._FactorNames = FactorNames
        return FactorNames
    @property
    def FactorNames(self):
        if self._FactorNames is None:
            self._FactorNames = self._getFactorNames(self.FactorField)
        if isinstance(self._FactorNames, dict):
            return sorted(self._FactorNames.keys())
        else:
            return self._FactorNames
    def getID(self, ifactor_name=None, idt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT DISTINCT "+self._getIDField(args=args)+" AS ID "
        SQLStr += self._genFromSQLStr()+" "
        if idt is not None: SQLStr += "WHERE "+DTField+"="+idt.strftime(self._DTFormat)+" "
        else: SQLStr += "WHERE "+DTField+" IS NOT NULL "
        SQLStr += "AND "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        DBFactorField = self._DBTableName+"."+self._FactorInfo.loc[FactorNameField, "DBFieldName"]
        if ifactor_name is not None:
            FactorNames = self._getFactorNames(FactorNameField, check_list=True)
            if isinstance(FactorNames, dict):
                ifactor_name = FactorNames[ifactor_name]
            if self.__QS_identifyDataType__(self._FactorInfo.loc[FactorNameField, "DataType"])!="double":
                SQLStr += "AND "+DBFactorField+"='"+ifactor_name+"' "
            else:
                SQLStr += "AND "+DBFactorField+"="+str(ifactor_name)+" "
        else:
            SQLStr += "AND "+DBFactorField+" IS NOT NULL "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)
        SQLStr += "ORDER BY ID"
        return self.__QS_restoreID__([iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)])
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT DISTINCT "+DTField+" "
        if iid is not None:
            SQLStr += self._genFromSQLStr()+" "
            SQLStr += "WHERE "+self._MainTableName+"."+self._MainTableID+"='"+self.__QS_adjustID__([iid])[0]+"' "
            if pd.notnull(self._MainTableCondition): SQLStr += "AND "+self._MainTableCondition+" "
        else:
            SQLStr += "FROM "+self._DBTableName+" "
            SQLStr += "WHERE "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        if start_dt is not None: SQLStr += "AND "+DTField+">="+start_dt.strftime(self._DTFormat)+" "
        if end_dt is not None: SQLStr += "AND "+DTField+"<="+end_dt.strftime(self._DTFormat)+" "
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        DBFactorField = self._DBTableName+"."+self._FactorInfo.loc[FactorNameField, "DBFieldName"]
        if ifactor_name is not None:
            FactorNames = self._getFactorNames(FactorNameField, check_list=True)
            if isinstance(FactorNames, dict):
                ifactor_name = FactorNames[ifactor_name]
            if self.__QS_identifyDataType__(self._FactorInfo.loc[FactorNameField, "DataType"])!="double":
                SQLStr += "AND "+DBFactorField+"='"+ifactor_name+"' "
            else:
                SQLStr += "AND "+DBFactorField+"="+str(ifactor_name)+" "
        else:
            SQLStr += "AND "+DBFactorField+" IS NOT NULL "
        SQLStr += self._genConditionSQLStr(use_main_table=False, args=args)+" "
        SQLStr += "ORDER BY "+DTField
        return [iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)]
    def _genNullIDSQLStr(self, factor_names, ids, end_date, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        DBFactorField = self._DBTableName+"."+self._FactorInfo.loc[FactorNameField, "DBFieldName"]
        FactorFieldStr = (self.__QS_identifyDataType__(self._FactorInfo.loc[FactorNameField, "DataType"])!="double")
        SubSQLStr = "SELECT "+self._MainTableName+"."+self._MainTableID+", "
        SubSQLStr += "MAX("+DTField+") "
        SubSQLStr += self._genFromSQLStr()+" "
        SubSQLStr += "WHERE "+DTField+"<"+end_date.strftime(self._DTFormat)+" "
        SubSQLStr += self._genIDSQLStr(ids, args=args)+" "
        ConditionSQLStr = self._genConditionSQLStr(use_main_table=True, args=args)
        SubSQLStr += ConditionSQLStr+" "
        SubSQLStr += "GROUP BY "+self._MainTableName+"."+self._MainTableID
        SQLStr = "SELECT "+DTField+", "
        SQLStr += self._getIDField(args=args)+" AS ID, "
        SQLStr += DBFactorField+", "
        SQLStr += self._DBTableName+"."+self._FactorInfo.loc[args.get("因子值字段", self.FactorValueField), "DBFieldName"]+" "
        SQLStr += self._genFromSQLStr()+" "
        SQLStr += "WHERE ("+self._MainTableName+"."+self._MainTableID+", "+DTField+") IN ("+SubSQLStr+") "
        FactorNames = self._getFactorNames(FactorNameField, check_list=True)
        if isinstance(FactorNames, list):
            SQLStr += "AND ("+genSQLInCondition(DBFactorField, factor_names, is_str=FactorFieldStr, max_num=1000)+") "
        elif isinstance(FactorNames, dict):
            SQLStr += "AND ("+genSQLInCondition(DBFactorField, [FactorNames[iFactor] for iFactor in factor_names], is_str=FactorFieldStr, max_num=1000)+") "
        SQLStr += ConditionSQLStr
        return SQLStr
    def _genSQLStr(self, factor_names, ids, start_date, end_date, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        DBFactorField = self._DBTableName+"."+self._FactorInfo.loc[FactorNameField, "DBFieldName"]
        FactorFieldStr = (self.__QS_identifyDataType__(self._FactorInfo.loc[FactorNameField, "DataType"])!="double")
        # 形成SQL语句, 日期, ID, 因子数据
        SQLStr = "SELECT "+DTField+", "
        SQLStr += self._getIDField(args=args)+" AS ID, "
        SQLStr += DBFactorField+", "
        SQLStr += self._DBTableName+"."+self._FactorInfo.loc[args.get("因子值字段", self.FactorValueField), "DBFieldName"]+" "
        SQLStr += self._genFromSQLStr()+" "
        SQLStr += "WHERE "+DTField+">="+start_date.strftime(self._DTFormat)+" "
        SQLStr += "AND "+DTField+"<="+end_date.strftime(self._DTFormat)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += self._genIDSQLStr(ids, args=args)+" "
        FactorNames = self._getFactorNames(FactorNameField, check_list=True)
        if isinstance(FactorNames, list):
            SQLStr += "AND ("+genSQLInCondition(DBFactorField, factor_names, is_str=FactorFieldStr, max_num=1000)+") "
        elif isinstance(FactorNames, dict):
            SQLStr += "AND ("+genSQLInCondition(DBFactorField, [FactorNames[iFactor] for iFactor in factor_names], is_str=FactorFieldStr, max_num=1000)+") "
        SQLStr += "ORDER BY ID, "+DTField+", "+DBFactorField
        return SQLStr
    def __QS_prepareRawData__(self, factor_names, ids, dts, args={}):
        StartDate, EndDate = dts[0].date(), dts[-1].date()
        LookBack = args.get("回溯天数", self.LookBack)
        if not np.isinf(LookBack): StartDate -= dt.timedelta(LookBack)
        FactorValueField = args.get("因子值字段", self.FactorValueField)
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        RawData = self._FactorDB.fetchall(self._genSQLStr(factor_names, ids, start_date=StartDate, end_date=EndDate, args=args))
        if not RawData: RawData = pd.DataFrame(columns=["QS_DT", "ID", FactorNameField, FactorValueField])
        RawData = pd.DataFrame(np.array(RawData, dtype="O"), columns=["QS_DT", "ID", FactorNameField, FactorValueField])
        if np.isinf(LookBack):
            NullIDs = set(ids).difference(set(RawData[RawData["QS_DT"]==dt.datetime.combine(StartDate,dt.time(0))]["ID"]))
            if NullIDs:
                NullRawData = self._FactorDB.fetchall(self._genNullIDSQLStr(factor_names, list(NullIDs), StartDate, args=args))
                if NullRawData:
                    NullRawData = pd.DataFrame(np.array(NullRawData, dtype="O"), columns=["QS_DT", "ID", FactorNameField, FactorValueField])
                    RawData = pd.concat([NullRawData, RawData], ignore_index=True)
                    RawData.sort_values(by=["ID", "QS_DT", FactorNameField])
        if RawData.shape[0]==0: return RawData
        return self._adjustRawDataByRelatedField(RawData, [FactorNameField, FactorValueField])
    def __QS_calcData__(self, raw_data, factor_names, ids, dts, args={}):
        if raw_data.shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
        FactorValueField = args.get("因子值字段", self.FactorValueField)
        FactorNameField = args.get("因子名字段", self.FactorNameField)
        raw_data = raw_data.set_index(["QS_DT", "ID", FactorNameField]).iloc[:, 0]
        raw_data = raw_data.unstack()
        isDouble = (self.__QS_identifyDataType__(self._FactorInfo.loc[FactorValueField, "DataType"])=="double")
        Data = {}
        for iFactorName in factor_names:
            if iFactorName in raw_data:
                iRawData = raw_data[iFactorName].unstack()
                if isDouble: iRawData = iRawData.astype("float")
                Data[iFactorName] = iRawData
        if not Data: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
        Data = pd.Panel(Data).loc[factor_names]
        LookBack = args.get("回溯天数", self.LookBack)
        return adjustDataDTID(Data, LookBack, factor_names, ids, dts, args.get("只起始日回溯", self.OnlyStartLookBack), logger=self._QS_Logger)


# 基于 SQL 数据库表的特征因子表
# 一个字段标识 ID, 其余字段为因子
class SQL_FeatureTable(SQL_Table):
    """SQL 特征因子表"""
    MultiMapping = Bool(False, label="多重映射", arg_type="Bool", order=0)
    Operator = Either(Function(None), None, arg_type="Function", label="算子", order=1)
    OperatorDataType = Enum("object", "double", "string", arg_type="SingleOption", label="算子数据类型", order=2)
    def __init__(self, name, fdb, sys_args={}, table_prefix="", table_info=None, factor_info=None, security_info=None, exchange_info=None, **kwargs):
        super().__init__(name=name, fdb=fdb, sys_args=sys_args, table_prefix=table_prefix, table_info=table_info, factor_info=factor_info, security_info=security_info, exchange_info=exchange_info, **kwargs)
        self._QS_IgnoredGroupArgs = ("遍历模式", "多重映射", "算子", "算子数据类型")
    def getID(self, ifactor_name=None, idt=None, args={}):
        SQLStr = "SELECT DISTINCT "+self._getIDField(args=args)+" AS ID "
        SQLStr += self._genFromSQLStr()+" "
        SQLStr += "WHERE "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID"
        return self.__QS_restoreID__([str(iRslt[0]) for iRslt in self._FactorDB.fetchall(SQLStr)])
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        return []
    def getFactorMetaData(self, factor_names=None, key=None, args={}):
        if key=="DataType":
            if factor_names is None: factor_names = self.FactorNames
            if args.get("算子", self.Operator) is None:
                if args.get("多重映射", self.MultiMapping):
                    return pd.Series(["object"]*len(factor_names), index=factor_names)
                else:
                    return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
            else:
                return pd.Series([args.get("算子数据类型", self.OperatorDataType)]*len(factor_names), index=factor_names)
        else:
            return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
    def __QS_prepareRawData__(self, factor_names, ids, dts, args={}):
        # 形成SQL语句, ID, 因子数据
        SQLStr = "SELECT "+self._getIDField(args=args)+" AS ID, "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        SQLStr += self._genIDSQLStr(ids, init_keyword="WHERE", args=args)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID"
        RawData = self._FactorDB.fetchall(SQLStr)
        if not RawData: return pd.DataFrame(columns=["ID"]+factor_names)
        RawData = pd.DataFrame(np.array(RawData, dtype="O"), columns=["ID"]+factor_names)
        RawData = self._adjustRawDataByRelatedField(RawData, factor_names)
        return RawData
    def __QS_calcData__(self, raw_data, factor_names, ids, dts, args={}):
        raw_data = raw_data.set_index(["ID"])
        if raw_data.index.intersection(ids).shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
        if args.get("多重映射", self.MultiMapping):
            Operator = args.get("算子", self.Operator)
            if Operator is None: Operator = (lambda x: x.tolist())
            Data = {}
            for iFactorName in factor_names:
                Data[iFactorName] = raw_data[iFactorName].groupby(axis=0, level=0).apply(Operator)
            Data = pd.DataFrame(Data).loc[:, factor_names]
        else:
            Data = raw_data.loc[:, factor_names]
            DupMask = Data.index.duplicated()
            if np.any(DupMask):
                self._QS_Logger.warning("%s 的表 %s 提取的数据中包含重复 ID: %s" % (self._FactorDB.Name, self.Name, str(Data.index[DupMask])))
                Data = Data[~DupMask]
        Data = pd.Panel(Data.values.T.reshape((Data.shape[1], Data.shape[0], 1)).repeat(len(dts), axis=2), items=factor_names, major_axis=Data.index, minor_axis=dts).swapaxes(1, 2)
        return Data.loc[:, :, ids]

# 映射因子表
# 一个字段标识 ID, 一个字段标识起始时点, 一个字段标识截止时点, 其余字段为因子
class SQL_MappingTable(SQL_Table):
    """映射因子表"""
    OnlyStartFilled = Bool(False, label="只填起始日", arg_type="Bool", order=0)
    MultiMapping = Bool(False, label="多重映射", arg_type="Bool", order=1)
    def __init__(self, name, fdb, sys_args={}, table_prefix="", table_info=None, factor_info=None, security_info=None, exchange_info=None, **kwargs):
        super().__init__(name=name, fdb=fdb, sys_args=sys_args, table_prefix=table_prefix, table_info=table_info, factor_info=factor_info, security_info=security_info, exchange_info=exchange_info, **kwargs)
        self._QS_IgnoredGroupArgs = ("遍历模式", "只填起始日", "多重映射")
        #self._StartDateField = self._FactorInfo["DBFieldName"][self._FactorInfo["FieldType"]=="Date"].iloc[0]
        self._EndDateField = self._FactorInfo["DBFieldName"][self._FactorInfo["FieldType"]=="EndDate"].iloc[0]
        self._EndDateIncluded = self._FactorInfo[self._FactorInfo["FieldType"]=="EndDate"]["Supplementary"].iloc[0]
        self._EndDateIncluded = (pd.isnull(self._EndDateIncluded) or (self._EndDateIncluded=="包含"))
    # 返回给定时点 idt 有数据的所有 ID
    # 如果 idt 为 None, 将返回所有有记录的 ID
    # 忽略 ifactor_name
    def getID(self, ifactor_name=None, idt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT DISTINCT "+self._getIDField(args=args)+" AS ID "
        SQLStr += self._genFromSQLStr()+" "
        if idt is not None:
            SQLStr += "WHERE "+DTField+"<="+idt.strftime(self._DTFormat)+" "
            if self._EndDateIncluded:
                SQLStr += "AND "+self._DBTableName+"."+self._EndDateField+">="+idt.strftime(self._DTFormat)+" "
            else:
                SQLStr += "AND "+self._DBTableName+"."+self._EndDateField+">"+idt.strftime(self._DTFormat)+" "
        else: SQLStr += "WHERE "+DTField+" IS NOT NULL "
        SQLStr += "AND "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "ORDER BY ID"
        return self.__QS_restoreID__([iRslt[0] for iRslt in self._FactorDB.fetchall(SQLStr)])
    # 返回给定 ID iid 的起始日期距今的时点序列
    # 如果 idt 为 None, 将以表中最小的起始日期作为起点
    # 忽略 ifactor_name    
    def getDateTime(self, ifactor_name=None, iid=None, start_dt=None, end_dt=None, args={}):
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        SQLStr = "SELECT MIN("+DTField+") "# 起始日期
        if iid is not None:
            SQLStr += self._genFromSQLStr()+" "
            SQLStr += "WHERE "+self._MainTableName+"."+self._MainTableID+"='"+self.__QS_adjustID__([iid])[0]+"' "
            SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        else:
            SQLStr += "FROM "+self._DBTableName+" "
            SQLStr += "WHERE "+self._DBTableName+"."+self._IDField+" IS NOT NULL "
            SQLStr += self._genConditionSQLStr(use_main_table=False, args=args)+" "
        StartDT = self._FactorDB.fetchall(SQLStr)[0][0]
        if start_dt is not None: StartDT = max((StartDT, start_dt))
        if end_dt is None: end_dt = dt.datetime.combine(dt.date.today(), dt.time(0))
        return getDateTimeSeries(start_dt=StartDT, end_dt=end_dt, timedelta=dt.timedelta(1))
    def getFactorMetaData(self, factor_names=None, key=None, args={}):
        if key=="DataType":
            if factor_names is None: factor_names = self.FactorNames
            if args.get("多重映射", self.MultiMapping):
                return pd.Series(["object"]*len(factor_names), index=factor_names)
            else:
                return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
        else:
            return super().getFactorMetaData(factor_names=factor_names, key=key, args=args)
    def __QS_prepareRawData__(self, factor_names, ids, dts, args={}):
        StartDate, EndDate = dts[0].date(), dts[-1].date()
        DTField = self._DBTableName+"."+self._FactorInfo.loc[args.get("时点字段", self.DTField), "DBFieldName"]
        # 形成SQL语句, ID, 开始日期, 结束日期, 因子数据
        SQLStr = "SELECT "+self._getIDField(args=args)+" AS ID, "
        SQLStr += DTField+", "
        SQLStr += self._DBTableName+"."+self._EndDateField+", "
        FieldSQLStr, SETableJoinStr = self._genFieldSQLStr(factor_names)
        SQLStr += FieldSQLStr+" "
        SQLStr += self._genFromSQLStr(setable_join_str=SETableJoinStr)+" "
        SQLStr += self._genIDSQLStr(ids, init_keyword="WHERE", args=args)+" "
        SQLStr += self._genConditionSQLStr(use_main_table=True, args=args)+" "
        SQLStr += "AND (("+self._DBTableName+"."+self._EndDateField+">="+StartDate.strftime(self._DTFormat)+") "
        SQLStr += "OR ("+self._DBTableName+"."+self._EndDateField+" IS NULL) "
        SQLStr += "OR ("+self._DBTableName+"."+self._EndDateField+"<"+DTField+")) "
        SQLStr += "AND "+DTField+"<="+EndDate.strftime(self._DTFormat)+" "
        SQLStr += "ORDER BY ID, "+DTField
        RawData = self._FactorDB.fetchall(SQLStr)
        if not RawData: return pd.DataFrame(columns=["ID", "QS_起始日", "QS_结束日"]+factor_names)
        RawData = pd.DataFrame(np.array(RawData, dtype="O"), columns=["ID", "QS_起始日", "QS_结束日"]+factor_names)
        RawData = self._adjustRawDataByRelatedField(RawData, factor_names)
        return RawData
    def _calcMultiMappingData(self, raw_data, factor_names, ids, dts, args={}):
        Data, nDT, nFactor = {}, len(dts), len(factor_names)
        raw_data.set_index(["ID"], inplace=True)
        raw_data["QS_结束日"] = raw_data["QS_结束日"].where(pd.notnull(raw_data["QS_结束日"]), dts[-1]+dt.timedelta(1))
        if args.get("只填起始日", self.OnlyStartFilled):
            if self._EndDateIncluded:
                raw_data["QS_结束日"] = (raw_data["QS_结束日"] + dt.timedelta(1)).astype("O")
            raw_data["QS_起始日"] = raw_data["QS_起始日"].where(raw_data["QS_起始日"]>=dts[0], dts[0])
            for iID in raw_data.index.unique():
                #iRawData = raw_data.loc[[iID]].set_index(["QS_起始日"])
                #iData = pd.DataFrame([([],)*nFactor]*nDT, index=dts, columns=factor_names, dtype="O")
                #for jStartDate in iRawData.index.drop_duplicates():
                    #iData.iloc[iData.index.searchsorted(jStartDate)] += pd.Series(iRawData.loc[[jStartDate], factor_names].values.T.tolist(), index=factor_names)
                #Data[iID] = iData
                iRawData = raw_data.loc[[iID]]
                iStartEndDates = sorted(pd.unique(np.r_[iRawData["QS_起始日"].values, iRawData["QS_结束日"].values]))
                iTempData = pd.DataFrame([([],)*nFactor]*len(iStartEndDates), index=iStartEndDates, columns=factor_names, dtype="O")
                iRawData = iRawData.set_index(["QS_起始日", "QS_结束日"])
                for jStartDate, jEndDate in iRawData.index.drop_duplicates():
                    ijRawData = iRawData.loc[jStartDate]
                    if pd.notnull(jEndDate):
                        ijRawData = ijRawData.loc[pd.notnull(ijRawData.index), factor_names]
                        ijRawData = ijRawData.loc[[jEndDate]].values.T.tolist()
                    else:
                        ijRawData = ijRawData.loc[pd.isnull(ijRawData.index), factor_names].values.T.tolist()
                    if jEndDate<jStartDate:
                        ijOldData = iTempData.loc[jStartDate:]
                        iTempData.loc[jStartDate:] += pd.DataFrame([ijRawData] * ijOldData.shape[0], index=ijOldData.index, columns=ijOldData.columns, dtype="O")
                    else:
                        jEndDate -= dt.timedelta(1)
                        ijOldData = iTempData.loc[jStartDate:jEndDate]
                        iTempData.loc[jStartDate:jEndDate] += pd.DataFrame([ijRawData] * ijOldData.shape[0], index=ijOldData.index, columns=ijOldData.columns, dtype="O")
                iData = pd.DataFrame([(None,)*nFactor]*nDT, index=dts, columns=factor_names, dtype="O")
                for j, jDate in enumerate(iStartEndDates):
                    jIdx = iData.index.searchsorted(jDate)
                    if jIdx<iData.shape[0]:
                        iData.iloc[jIdx] = iTempData.iloc[j]
                Data[iID] = iData
            return pd.Panel(Data).swapaxes(0, 2).loc[:, :, ids]
        else:
            DeltaDT = dt.timedelta(int(not self._EndDateIncluded))
            for iID in raw_data.index.unique():
                iRawData = raw_data.loc[[iID]].set_index(["QS_起始日", "QS_结束日"])
                iData = pd.DataFrame([([],)*nFactor]*nDT, index=dts, columns=factor_names, dtype="O")
                for jStartDate, jEndDate in iRawData.index.drop_duplicates():
                    ijRawData = iRawData.loc[jStartDate]
                    if pd.notnull(jEndDate):
                        ijRawData = ijRawData.loc[pd.notnull(ijRawData.index), factor_names]
                        ijRawData = ijRawData.loc[[jEndDate]].values.T.tolist()
                    else:
                        ijRawData = ijRawData.loc[pd.isnull(ijRawData.index), factor_names].values.T.tolist()
                    if jEndDate<jStartDate:
                        ijOldData = iData.loc[jStartDate:]
                        iData.loc[jStartDate:] += pd.DataFrame([ijRawData] * ijOldData.shape[0], index=ijOldData.index, columns=ijOldData.columns, dtype="O")
                    else:
                        jEndDate -= DeltaDT
                        ijOldData = iData.loc[jStartDate:jEndDate]
                        iData.loc[jStartDate:jEndDate] += pd.DataFrame([ijRawData] * ijOldData.shape[0], index=ijOldData.index, columns=ijOldData.columns, dtype="O")
                Data[iID] = iData
            return pd.Panel(Data).swapaxes(0, 2).loc[:, :, ids]
    def __QS_calcData__(self, raw_data, factor_names, ids, dts, args={}):
        if raw_data.shape[0]==0: return pd.Panel(items=factor_names, major_axis=dts, minor_axis=ids)
        if args.get("多重映射", self.MultiMapping): return self._calcMultiMappingData(raw_data, factor_names, ids, dts, args=args)
        raw_data.set_index(["ID"], inplace=True)
        Data, nFactor = {}, len(factor_names)
        raw_data["QS_结束日"] = raw_data["QS_结束日"].where(pd.notnull(raw_data["QS_结束日"]), dts[-1]+dt.timedelta(1))
        if args.get("只填起始日", self.OnlyStartFilled):
            if self._EndDateIncluded:
                raw_data["QS_结束日"] = (raw_data["QS_结束日"] + dt.timedelta(1)).astype("O")
            raw_data["QS_起始日"] = raw_data["QS_起始日"].where(raw_data["QS_起始日"]>=dts[0], dts[0])
            for iID in raw_data.index.unique():
                #iRawData = raw_data.loc[[iID]].set_index(["QS_起始日"])
                #iData = pd.DataFrame(index=dts, columns=factor_names)
                #for jStartDate in iRawData.index:
                    #iData.iloc[iData.index.searchsorted(jStartDate)] = iRawData.loc[jStartDate, factor_names]
                #Data[iID] = iData
                iRawData = raw_data.loc[[iID]]
                iStartEndDates = np.r_[iRawData["QS_起始日"].values, iRawData["QS_结束日"].values]
                iStartEndDates = sorted(pd.unique(iStartEndDates[pd.notnull(iStartEndDates)]))
                iTempData = pd.DataFrame(index=iStartEndDates, columns=factor_names)
                #iRawData = iRawData.set_index(["QS_起始日", "QS_结束日"])
                for j in range(iRawData.shape[0]):
                    ijRawData = iRawData.iloc[j]
                    jStartDate, jEndDate = ijRawData["QS_起始日"], ijRawData["QS_结束日"]
                    if jEndDate<jStartDate:
                        iTempData.loc[jStartDate:] = np.repeat(ijRawData[factor_names].values.reshape((1, nFactor)), iTempData.loc[jStartDate:].shape[0], axis=0)
                    else:
                        jEndDate -= dt.timedelta(1)
                        iTempData.loc[jStartDate:jEndDate] = np.repeat(ijRawData[factor_names].values.reshape((1, nFactor)), iTempData.loc[jStartDate:jEndDate].shape[0], axis=0)
                iData = pd.DataFrame(index=dts, columns=factor_names)
                for j, jDate in enumerate(iStartEndDates):
                    jIdx = iData.index.searchsorted(jDate)
                    if jIdx<iData.shape[0]:
                        iData.iloc[jIdx] = iTempData.iloc[j]
                Data[iID] = iData
            return pd.Panel(Data).swapaxes(0, 2).loc[:, :, ids]
        else:
            DeltaDT = dt.timedelta(int(not self._EndDateIncluded))
            for iID in raw_data.index.unique():
                iRawData = raw_data.loc[[iID]]
                iData = pd.DataFrame(index=dts, columns=factor_names)
                for j in range(iRawData.shape[0]):
                    ijRawData = iRawData.iloc[j]
                    jStartDate, jEndDate = ijRawData["QS_起始日"], ijRawData["QS_结束日"]
                    if jEndDate<jStartDate:
                        iData.loc[jStartDate:] = np.repeat(ijRawData[factor_names].values.reshape((1, nFactor)), iData.loc[jStartDate:].shape[0], axis=0)
                    else:
                        jEndDate -= DeltaDT
                        iData.loc[jStartDate:jEndDate] = np.repeat(ijRawData[factor_names].values.reshape((1, nFactor)), iData.loc[jStartDate:jEndDate].shape[0], axis=0)
                Data[iID] = iData
            return pd.Panel(Data).swapaxes(0, 2).loc[:, :, ids]
